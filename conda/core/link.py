# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict, namedtuple
from logging import getLogger
import os
from os.path import basename, dirname, isdir, join
import sys
from traceback import format_exception_only
import warnings

from .package_cache_data import PackageCacheData
from .path_actions import (CompileMultiPycAction, CreateNonadminAction, CreatePrefixRecordAction,
                           CreatePythonEntryPointAction, LinkPathAction, MakeMenuAction,
                           RegisterEnvironmentLocationAction, RemoveLinkedPackageRecordAction,
                           RemoveMenuAction, UnlinkPathAction, UnregisterEnvironmentLocationAction,
                           UpdateHistoryAction, AggregateCompileMultiPycAction)
from .prefix_data import PrefixData, get_python_version_for_prefix
from .. import CondaError, CondaMultiError, conda_signal_handler
from .._vendor.auxlib.collection import first
from .._vendor.auxlib.ish import dals
from .._vendor.toolz import concat, concatv, interleave
from ..base.constants import DEFAULTS_CHANNEL_NAME, PREFIX_MAGIC_FILE, SafetyChecks
from ..base.context import context
from ..common.compat import ensure_text_type, iteritems, itervalues, odict, on_win, text_type
from ..common.io import Spinner, dashlist, time_recorder
from ..common.io import DummyExecutor, ThreadLimitedThreadPoolExecutor, as_completed
from ..common.path import (explode_directories, get_all_directories, get_major_minor_version,
                           get_python_site_packages_short_path)
from ..common.signals import signal_handler
from ..exceptions import (DisallowedPackageError, EnvironmentNotWritableError,
                          KnownPackageClobberError, LinkError, RemoveError,
                          SharedLinkPathClobberError, UnknownPackageClobberError, maybe_raise)
from ..gateways.disk import mkdir_p
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.read import isfile, lexists, read_package_info
from ..gateways.disk.test import hardlink_supported, is_conda_environment, softlink_supported
from ..gateways.subprocess import subprocess_call
from ..models.enums import LinkType
from ..models.version import VersionOrder
from ..resolve import MatchSpec
from ..utils import ensure_comspec_set, human_bytes, wrap_subprocess_call

log = getLogger(__name__)


def determine_link_type(extracted_package_dir, target_prefix):
    source_test_file = join(extracted_package_dir, 'info', 'index.json')
    if context.always_copy:
        return LinkType.copy
    if context.always_softlink:
        return LinkType.softlink
    if hardlink_supported(source_test_file, target_prefix):
        return LinkType.hardlink
    if context.allow_softlinks and softlink_supported(source_test_file, target_prefix):
        return LinkType.softlink
    return LinkType.copy


def make_unlink_actions(transaction_context, target_prefix, prefix_record):
    # no side effects in this function!
    unlink_path_actions = tuple(UnlinkPathAction(transaction_context, prefix_record,
                                                 target_prefix, trgt)
                                for trgt in prefix_record.files)

    remove_menu_actions = RemoveMenuAction.create_actions(transaction_context,
                                                          prefix_record,
                                                          target_prefix)

    try:
        extracted_package_dir = basename(prefix_record.extracted_package_dir)
    except AttributeError:
        try:
            extracted_package_dir = basename(prefix_record.link.source)
        except AttributeError:
            # for backward compatibility only
            extracted_package_dir = '%s-%s-%s' % (prefix_record.name, prefix_record.version,
                                                  prefix_record.build)

    meta_short_path = '%s/%s' % ('conda-meta', extracted_package_dir + '.json')
    remove_conda_meta_actions = (RemoveLinkedPackageRecordAction(transaction_context,
                                                                 prefix_record,
                                                                 target_prefix, meta_short_path),)

    _all_d = get_all_directories(axn.target_short_path for axn in unlink_path_actions)
    all_directories = sorted(explode_directories(_all_d, already_split=True), reverse=True)
    directory_remove_actions = tuple(UnlinkPathAction(transaction_context, prefix_record,
                                                      target_prefix, d, LinkType.directory)
                                     for d in all_directories)

    # unregister_private_package_actions = UnregisterPrivateEnvAction.create_actions(
    #     transaction_context, package_cache_record, target_prefix
    # )

    return tuple(concatv(
        remove_menu_actions,
        unlink_path_actions,
        directory_remove_actions,
        # unregister_private_package_actions,
        remove_conda_meta_actions,
    ))


def match_specs_to_dists(packages_info_to_link, specs):
    matched_specs = [None for _ in range(len(packages_info_to_link))]
    for spec in specs or ():
        spec = MatchSpec(spec)
        idx = next((q for q, pkg_info in enumerate(packages_info_to_link)
                    if pkg_info.repodata_record.name == spec.name),
                   None)
        if idx is not None:
            matched_specs[idx] = spec
    return tuple(matched_specs)


PrefixSetup = namedtuple('PrefixSetup', (
    'target_prefix',
    'unlink_precs',
    'link_precs',
    'remove_specs',
    'update_specs',
))

PrefixActionGroup = namedtuple('PrefixActionGroup', (
    'unlink_action_groups',
    'unregister_action_groups',
    'link_action_groups',
    'register_action_groups',
    'compile_action_groups',
    'entry_point_action_groups',
    'prefix_record_groups',
))

# each PrefixGroup item is a sequence of ActionGroups
ActionGroup = namedtuple('ActionGroup', (
    'type',
    'pkg_data',
    'actions',
    'target_prefix',
))

ChangeReport = namedtuple("ChangeReport", (
    "prefix",
    "specs_to_remove",
    "specs_to_add",
    "removed_precs",
    "new_precs",
    "updated_precs",
    "downgraded_precs",
    "superseded_precs",
    "fetch_precs",
))


class UnlinkLinkTransaction(object):

    def __init__(self, *setups):
        self.prefix_setups = odict((stp.target_prefix, stp) for stp in setups)
        self.prefix_action_groups = odict()

        for stp in itervalues(self.prefix_setups):
            log.info("initializing UnlinkLinkTransaction with\n"
                     "  target_prefix: %s\n"
                     "  unlink_precs:\n"
                     "    %s\n"
                     "  link_precs:\n"
                     "    %s\n",
                     stp.target_prefix,
                     '\n    '.join(prec.dist_str() for prec in stp.unlink_precs),
                     '\n    '.join(prec.dist_str() for prec in stp.link_precs))

        self._pfe = None
        self._prepared = False
        self._verified = False
        self.executor = DummyExecutor() if context.debug else ThreadLimitedThreadPoolExecutor()

    @property
    def nothing_to_do(self):
        return (
            not any((stp.unlink_precs or stp.link_precs) for stp in itervalues(self.prefix_setups))
            and all(is_conda_environment(stp.target_prefix)
                    for stp in itervalues(self.prefix_setups))
        )

    def download_and_extract(self):
        if self._pfe is None:
            self._get_pfe()
        if not self._pfe._executed:
            self._pfe.execute()

    def prepare(self):
        if self._pfe is None:
            self._get_pfe()
        if not self._pfe._executed:
            self._pfe.execute()

        if self._prepared:
            return

        self.transaction_context = {}

        with Spinner("Preparing transaction", not context.verbosity and not context.quiet,
                     context.json):
            for stp in itervalues(self.prefix_setups):
                grps = self._prepare(self.transaction_context, stp.target_prefix,
                                     stp.unlink_precs, stp.link_precs,
                                     stp.remove_specs, stp.update_specs)
                self.prefix_action_groups[stp.target_prefix] = PrefixActionGroup(*grps)

        self._prepared = True

    @time_recorder("unlink_link_prepare_and_verify")
    def verify(self):
        if not self._prepared:
            self.prepare()

        assert not context.dry_run

        if context.safety_checks == SafetyChecks.disabled:
            self._verified = True
            return

        with Spinner("Verifying transaction", not context.verbosity and not context.quiet,
                     context.json):
            exceptions = self._verify(self.prefix_setups, self.prefix_action_groups)
            if exceptions:
                try:
                    maybe_raise(CondaMultiError(exceptions), context)
                except:
                    rm_rf(self.transaction_context['temp_dir'])
                    raise
                log.info(exceptions)

        self._verified = True

    def execute(self):
        if not self._verified:
            self.verify()

        assert not context.dry_run

        try:
            self._execute(tuple(concat(interleave(itervalues(self.prefix_action_groups)))))
        finally:
            rm_rf(self.transaction_context['temp_dir'])

    def _get_pfe(self):
        from .package_cache_data import ProgressiveFetchExtract
        if self._pfe is not None:
            pfe = self._pfe
        elif not self.prefix_setups:
            self._pfe = pfe = ProgressiveFetchExtract(())
        else:
            link_precs = set(concat(stp.link_precs for stp in itervalues(self.prefix_setups)))
            self._pfe = pfe = ProgressiveFetchExtract(link_precs)
        return pfe

    @classmethod
    def _prepare(cls, transaction_context, target_prefix, unlink_precs, link_precs,
                 remove_specs, update_specs):

        # make sure prefix directory exists
        if not isdir(target_prefix):
            try:
                mkdir_p(target_prefix)
            except (IOError, OSError) as e:
                log.debug(repr(e))
                raise CondaError("Unable to create prefix directory '%s'.\n"
                                 "Check that you have sufficient permissions."
                                 "" % target_prefix)

        # gather information from disk and caches
        prefix_data = PrefixData(target_prefix)
        prefix_recs_to_unlink = (prefix_data.get(prec.name) for prec in unlink_precs)
        # NOTE: load_meta can return None
        # TODO: figure out if this filter shouldn't be an assert not None
        prefix_recs_to_unlink = tuple(lpd for lpd in prefix_recs_to_unlink if lpd)
        pkg_cache_recs_to_link = tuple(PackageCacheData.get_entry_to_link(prec)
                                       for prec in link_precs)
        assert all(pkg_cache_recs_to_link)
        packages_info_to_link = tuple(read_package_info(prec, pcrec)
                                      for prec, pcrec in zip(link_precs, pkg_cache_recs_to_link))

        link_types = tuple(determine_link_type(pkg_info.extracted_package_dir, target_prefix)
                           for pkg_info in packages_info_to_link)

        # make all the path actions
        # no side effects allowed when instantiating these action objects
        python_version = cls._get_python_version(target_prefix,
                                                 prefix_recs_to_unlink,
                                                 packages_info_to_link)
        transaction_context['target_python_version'] = python_version
        sp = get_python_site_packages_short_path(python_version)
        transaction_context['target_site_packages_short_path'] = sp

        transaction_context['temp_dir'] = join(target_prefix, '.condatmp')

        unlink_action_groups = tuple(ActionGroup(
            'unlink',
            prefix_rec,
            make_unlink_actions(transaction_context, target_prefix, prefix_rec),
            target_prefix,
        ) for prefix_rec in prefix_recs_to_unlink)

        if unlink_action_groups:
            axns = UnregisterEnvironmentLocationAction(transaction_context, target_prefix),
            unregister_action_groups = ActionGroup('unregister', None, axns, target_prefix),
        else:
            unregister_action_groups = ()

        matchspecs_for_link_dists = match_specs_to_dists(packages_info_to_link, update_specs)
        link_action_groups = tuple(
            ActionGroup('link', pkg_info, cls._make_link_actions(transaction_context, pkg_info,
                                                                 target_prefix, lt, spec),
                        target_prefix)
            for pkg_info, lt, spec in zip(packages_info_to_link, link_types,
                                          matchspecs_for_link_dists)
        )

        entry_point_action_groups = tuple(
            ActionGroup('entry_point', pkg_info, cls._make_entry_point_actions(
                transaction_context, pkg_info, target_prefix, lt, spec, link_action_groups),
                        target_prefix)
            for pkg_info, lt, spec in zip(packages_info_to_link, link_types,
                                          matchspecs_for_link_dists)
        )

        compile_action_groups = tuple(
            ActionGroup('compile', pkg_info, cls._make_compile_actions(
                transaction_context, pkg_info, target_prefix, lt, spec, link_action_groups),
                        target_prefix)
            for pkg_info, lt, spec in zip(packages_info_to_link, link_types,
                                          matchspecs_for_link_dists)
        )

        record_axns = []
        for pkg_info, lt, spec in zip(packages_info_to_link, link_types,
                                      matchspecs_for_link_dists):
            link_ag = next(ag for ag in link_action_groups if ag.pkg_data == pkg_info)
            compile_ag = next(ag for ag in compile_action_groups if ag.pkg_data == pkg_info)
            entry_point_ag = next(ag for ag in entry_point_action_groups
                                  if ag.pkg_data == pkg_info)
            all_link_path_actions = concatv(link_ag.actions,
                                            compile_ag.actions,
                                            entry_point_ag.actions)
            record_axns.extend(CreatePrefixRecordAction.create_actions(
                transaction_context, pkg_info, target_prefix, lt, spec, all_link_path_actions))
        prefix_record_groups = [ActionGroup('record', None, record_axns, target_prefix)]

        history_actions = UpdateHistoryAction.create_actions(
            transaction_context, target_prefix, remove_specs, update_specs,
        )
        register_actions = RegisterEnvironmentLocationAction(transaction_context, target_prefix),

        register_action_groups = ActionGroup('register', None,
                                             register_actions + history_actions,
                                             target_prefix),
        return PrefixActionGroup(
            unlink_action_groups,
            unregister_action_groups,
            link_action_groups,
            register_action_groups,
            compile_action_groups,
            entry_point_action_groups,
            prefix_record_groups,
        )

    @staticmethod
    def _verify_individual_level(prefix_action_group):
        all_actions = concat(axngroup.actions
                             for action_groups in prefix_action_group
                             for axngroup in action_groups)

        # run all per-action verify methods
        #   one of the more important of these checks is to verify that a file listed in
        #   the packages manifest (i.e. info/files) is actually contained within the package
        error_results = []
        for axn in all_actions:
            if axn.verified:
                continue
            error_result = axn.verify()
            if error_result:
                formatted_error = ''.join(format_exception_only(type(error_result), error_result))
                log.debug("Verification error in action %s\n%s", axn, formatted_error)
                error_results.append(error_result)
        return error_results

    @staticmethod
    def _verify_prefix_level(target_prefix, prefix_action_group):
        # further verification of the whole transaction
        # for each path we are creating in link_actions, we need to make sure
        #   1. each path either doesn't already exist in the prefix, or will be unlinked
        #   2. there's only a single instance of each path
        #   3. if the target is a private env, leased paths need to be verified
        #   4. make sure conda-meta/history file is writable
        #   5. make sure envs/catalog.json is writable; done with RegisterEnvironmentLocationAction
        # TODO: 3, 4

        unlink_action_groups = prefix_action_group.unlink_action_groups
        prefix_record_groups = prefix_action_group.prefix_record_groups

        lower_on_win = lambda p: p.lower() if on_win else p
        unlink_paths = set(lower_on_win(axn.target_short_path)
                           for grp in unlink_action_groups
                           for axn in grp.actions
                           if isinstance(axn, UnlinkPathAction))
        # we can get all of the paths being linked by looking only at the
        #   CreateLinkedPackageRecordAction actions
        create_lpr_actions = (axn
                              for grp in prefix_record_groups
                              for axn in grp.actions
                              if isinstance(axn, CreatePrefixRecordAction))

        error_results = []
        # Verification 1. each path either doesn't already exist in the prefix, or will be unlinked
        link_paths_dict = defaultdict(list)
        for axn in create_lpr_actions:
            for link_path_action in axn.all_link_path_actions:
                if isinstance(link_path_action, CompileMultiPycAction):
                    target_short_paths = link_path_action.target_short_paths
                elif isinstance(link_path_action, CreateNonadminAction):
                    continue
                else:
                    target_short_paths = ((link_path_action.target_short_path, )
                                          if not hasattr(link_path_action, 'link_type') or
                                          link_path_action.link_type != LinkType.directory
                                          else tuple())
                for path in target_short_paths:
                    path = lower_on_win(path)
                    link_paths_dict[path].append(axn)
                    if path not in unlink_paths and lexists(join(target_prefix, path)):
                        # we have a collision; at least try to figure out where it came from
                        colliding_prefix_rec = first(
                            (prefix_rec for prefix_rec in
                             PrefixData(target_prefix).iter_records()),
                            key=lambda prefix_rec: path in prefix_rec.files
                        )
                        if colliding_prefix_rec:
                            error_results.append(KnownPackageClobberError(
                                path,
                                axn.package_info.repodata_record.dist_str(),
                                colliding_prefix_rec.dist_str(),
                                context,
                            ))
                        else:
                            error_results.append(UnknownPackageClobberError(
                                path,
                                axn.package_info.repodata_record.dist_str(),
                                context,
                            ))

        # Verification 2. there's only a single instance of each path
        for path, axns in iteritems(link_paths_dict):
            if len(axns) > 1:
                error_results.append(SharedLinkPathClobberError(
                    path,
                    tuple(axn.package_info.repodata_record.dist_str() for axn in axns),
                    context,
                ))
        return error_results

    @staticmethod
    def _verify_transaction_level(prefix_setups):
        # 1. make sure we're not removing conda from conda's env
        # 2. make sure we're not removing a conda dependency from conda's env
        # 3. enforce context.disallowed_packages
        # 4. make sure we're not removing pinned packages without no-pin flag
        # 5. make sure conda-meta/history for each prefix is writable
        # TODO: Verification 4

        conda_prefixes = (join(context.root_prefix, 'envs', '_conda_'), context.root_prefix)
        conda_setups = tuple(setup for setup in itervalues(prefix_setups)
                             if setup.target_prefix in conda_prefixes)

        conda_unlinked = any(prec.name == 'conda'
                             for setup in conda_setups
                             for prec in setup.unlink_precs)

        conda_prec, conda_final_setup = next(
            ((prec, setup)
             for setup in conda_setups
             for prec in setup.link_precs
             if prec.name == 'conda'),
            (None, None)
        )

        if conda_unlinked and conda_final_setup is None:
            # means conda is being unlinked and not re-linked anywhere
            # this should never be able to be skipped, even with --force
            yield RemoveError("This operation will remove conda without replacing it with\n"
                              "another version of conda.")

        if conda_final_setup is None:
            # means we're not unlinking then linking a new package, so look up current conda record
            conda_final_prefix = context.conda_prefix
            pd = PrefixData(conda_final_prefix)
            pkg_names_already_lnkd = tuple(rec.name for rec in pd.iter_records())
            pkg_names_being_lnkd = ()
            pkg_names_being_unlnkd = ()
            conda_linked_depends = next(
                (record.depends for record in pd.iter_records() if record.name == 'conda'),
                ()
            )
        else:
            conda_final_prefix = conda_final_setup.target_prefix
            pd = PrefixData(conda_final_prefix)
            pkg_names_already_lnkd = tuple(rec.name for rec in pd.iter_records())
            pkg_names_being_lnkd = tuple(prec.name for prec in conda_final_setup.link_precs or ())
            pkg_names_being_unlnkd = tuple(prec.name for prec in conda_final_setup.unlink_precs
                                           or ())
            conda_linked_depends = conda_prec.depends

        for conda_dependency in conda_linked_depends:
            dep_name = MatchSpec(conda_dependency).name
            if dep_name not in pkg_names_being_lnkd and (dep_name not in pkg_names_already_lnkd
                                                         or dep_name in pkg_names_being_unlnkd):
                # equivalent to not (dep_name in pkg_names_being_lnkd or
                #  (dep_name in pkg_names_already_lnkd and dep_name not in pkg_names_being_unlnkd))
                yield RemoveError("'%s' is a dependency of conda and cannot be removed from\n"
                                  "conda's operating environment." % dep_name)

        # Verification 3. enforce disallowed_packages
        disallowed = tuple(MatchSpec(s) for s in context.disallowed_packages)
        for prefix_setup in itervalues(prefix_setups):
            for prec in prefix_setup.link_precs:
                if any(d.match(prec) for d in disallowed):
                    yield DisallowedPackageError(prec)

        # Verification 5. make sure conda-meta/history for each prefix is writable
        for prefix_setup in itervalues(prefix_setups):
            test_path = join(prefix_setup.target_prefix, PREFIX_MAGIC_FILE)
            test_path_existed = lexists(test_path)
            dir_existed = None
            try:
                dir_existed = mkdir_p(dirname(test_path))
                open(test_path, "a").close()
            except EnvironmentError:
                if dir_existed is False:
                    rm_rf(dirname(test_path))
                yield EnvironmentNotWritableError(prefix_setup.target_prefix)
            else:
                if not dir_existed:
                    rm_rf(dirname(test_path))
                elif not test_path_existed:
                    rm_rf(test_path)

    def _verify(self, prefix_setups, prefix_action_groups):
        transaction_exceptions = tuple(
            exc for exc in UnlinkLinkTransaction._verify_transaction_level(prefix_setups) if exc
        )
        if transaction_exceptions:
            return transaction_exceptions

        exceptions = []
        futures = list(self.executor.submit(UnlinkLinkTransaction._verify_individual_level, pg)
                       for pg in itervalues(prefix_action_groups))
        futures.extend(
            self.executor.submit(UnlinkLinkTransaction._verify_prefix_level, target_prefix, pg)
            for target_prefix, pg in iteritems(prefix_action_groups))
        for future in as_completed(futures):
            if future.result():
                exceptions.extend(future.result())
        return exceptions

    def _execute(self, all_action_groups):
        # unlink unlink_action_groups and unregister_action_groups
        unlink_actions = tuple(group for group in all_action_groups if group.type == "unlink")
        # link unlink_action_groups and register_action_groups
        link_actions = list(group for group in all_action_groups if group.type == "link")
        compile_actions = list(group for group in all_action_groups if group.type == "compile")
        entry_point_actions = list(group for group in all_action_groups
                                   if group.type == "entry_point")
        record_actions = list(group for group in all_action_groups if group.type == "record")

        with signal_handler(conda_signal_handler), time_recorder("unlink_link_execute"):
            exceptions = []
            with Spinner("Executing transaction", not context.verbosity and not context.quiet,
                         context.json):

                # Execute unlink actions
                for (group, register_group, install_side) in (
                        (unlink_actions, "unregister", False),
                        (link_actions, "register", True)):

                    for axngroup in group:
                        is_unlink = axngroup.type == 'unlink'
                        target_prefix = axngroup.target_prefix
                        prec = axngroup.pkg_data
                        run_script(target_prefix if is_unlink else prec.extracted_package_dir,
                                   prec,
                                   'pre-unlink' if is_unlink else 'pre-link',
                                   target_prefix)

                    # parallel block 1:
                    futures = (
                        self.executor.submit(UnlinkLinkTransaction._execute_actions, axngroup)
                        for axngroup in group)
                    for future in as_completed(futures):
                        exc = future.result()
                        if exc:
                            exceptions.append(exc)

                    # Run post-link or post-unlink scripts and registering AFTER link/unlink,
                    #    because they may depend on files in the prefix.  Additionally, run
                    #    them serially, just in case order matters (hopefully not)
                    for axngroup in group:
                        exc = UnlinkLinkTransaction._execute_post_link_actions(axngroup)
                        if exc:
                            exceptions.append(exc)

                    # parallel block 2:
                    futures = []
                    if install_side:
                        futures.extend(
                            self.executor.submit(UnlinkLinkTransaction._execute_actions, axngroup)
                            for axngroup in entry_point_actions)

                        # consolidate compile actions into one big'un for better efficiency
                        individual_actions = [axn for ag in compile_actions for axn in ag.actions]
                        if individual_actions:
                            composite = AggregateCompileMultiPycAction(*individual_actions)
                            ag = ActionGroup('compile', None, [composite], composite.target_prefix)
                            futures.append(
                                self.executor.submit(UnlinkLinkTransaction._execute_actions, ag))
                        futures.extend(
                            self.executor.submit(UnlinkLinkTransaction._execute_actions, axngroup)
                            for axngroup in record_actions)
                    for future in as_completed(futures):
                        exc = future.result()
                        if exc:
                            exceptions.append(exc)

                    # must do the register actions AFTER all link/unlink is done
                    register_actions = tuple(group for group in all_action_groups
                                             if group.type == register_group)
                    for axngroup in register_actions:
                        exc = UnlinkLinkTransaction._execute_actions(axngroup)
                        if exc:
                            exceptions.append(exc)
                    if exceptions:
                        break
            if exceptions:
                # might be good to show all errors, but right now we only show the first
                e = exceptions[0]
                axngroup = e.errors[1]

                action, is_unlink = (None, axngroup.type == 'unlink')
                prec = axngroup.pkg_data

                if prec:
                    log.error("An error occurred while %s package '%s'." % (
                            'uninstalling' if is_unlink else 'installing',
                            prec.dist_str()))

                # reverse all executed packages except the one that failed
                rollback_excs = []
                if context.rollback_enabled:
                    with Spinner("Rolling back transaction",
                                 not context.verbosity and not context.quiet, context.json):
                        reverse_actions = reversed(tuple(all_action_groups))
                        for axngroup in reverse_actions:
                            excs = UnlinkLinkTransaction._reverse_actions(axngroup)
                            rollback_excs.extend(excs)

                raise CondaMultiError(tuple(concatv(
                    ((e.errors[0], e.errors[2:])
                     if isinstance(e, CondaMultiError)
                     else (e,)),
                    rollback_excs,
                )))
            else:

                link_actions = tuple(
                    group for group in all_action_groups if group.type in ("link", "register"))
                for axngroup in all_action_groups:
                    for action in axngroup.actions:
                        action.cleanup()

    @staticmethod
    def _execute_actions(axngroup):
        target_prefix = axngroup.target_prefix
        action = None
        prec = axngroup.pkg_data

        conda_meta_dir = join(target_prefix, 'conda-meta')
        if not isdir(conda_meta_dir):
            mkdir_p(conda_meta_dir)

        try:
            if axngroup.type == 'unlink':
                log.info("===> UNLINKING PACKAGE: %s <===\n"
                         "  prefix=%s\n",
                         prec.dist_str(), target_prefix)

            elif axngroup.type == 'link':
                log.info("===> LINKING PACKAGE: %s <===\n"
                         "  prefix=%s\n"
                         "  source=%s\n",
                         prec.dist_str(), target_prefix, prec.extracted_package_dir)

            for action in axngroup.actions:
                action.execute()
        except Exception as e:  # this won't be a multi error
            # reverse this package
            reverse_excs = ()
            if context.rollback_enabled:
                reverse_excs = UnlinkLinkTransaction._reverse_actions(axngroup)
            return CondaMultiError(tuple(concatv(
                (e,),
                (axngroup,),
                reverse_excs,
            )))

    @staticmethod
    def _execute_post_link_actions(axngroup):
        target_prefix = axngroup.target_prefix
        is_unlink = axngroup.type == 'unlink'
        prec = axngroup.pkg_data
        if prec:
            try:
                run_script(target_prefix, prec, 'post-unlink' if is_unlink else 'post-link',
                           activate=True)
            except Exception as e:  # this won't be a multi error
                # reverse this package
                reverse_excs = ()
                if context.rollback_enabled:
                    reverse_excs = UnlinkLinkTransaction._reverse_actions(axngroup)
                return CondaMultiError(tuple(concatv(
                    (e,),
                    (axngroup,),
                    reverse_excs,
                )))

    @staticmethod
    def _reverse_actions(axngroup, reverse_from_idx=-1):
        target_prefix = axngroup.target_prefix

        # reverse_from_idx = -1 means reverse all actions
        prec = axngroup.pkg_data

        if axngroup.type == 'unlink':
            log.info("===> REVERSING PACKAGE UNLINK: %s <===\n"
                     "  prefix=%s\n", prec.dist_str(), target_prefix)

        elif axngroup.type == 'link':
            log.info("===> REVERSING PACKAGE LINK: %s <===\n"
                     "  prefix=%s\n", prec.dist_str(), target_prefix)

        exceptions = []
        if reverse_from_idx < 0:
            reverse_actions = axngroup.actions
        else:
            reverse_actions = axngroup.actions[:reverse_from_idx+1]
        for axn_idx, action in reversed(tuple(enumerate(reverse_actions))):
            try:
                action.reverse()
            except Exception as e:
                log.debug("action.reverse() error in action %r", action, exc_info=True)
                exceptions.append(e)
        return exceptions

    @staticmethod
    def _get_python_version(target_prefix, pcrecs_to_unlink, packages_info_to_link):
        # this method determines the python version that will be present at the
        # end of the transaction
        linking_new_python = next((package_info for package_info in packages_info_to_link
                                   if package_info.repodata_record.name == 'python'),
                                  None)
        if linking_new_python:
            # is python being linked? we're done
            full_version = linking_new_python.repodata_record.version
            assert full_version
            log.debug("found in current transaction python version %s", full_version)
            return get_major_minor_version(full_version)

        # is python already linked and not being unlinked? that's ok too
        linked_python_version = get_python_version_for_prefix(target_prefix)
        if linked_python_version:
            find_python = (lnkd_pkg_data for lnkd_pkg_data in pcrecs_to_unlink
                           if lnkd_pkg_data.name == 'python')
            unlinking_this_python = next(find_python, None)
            if unlinking_this_python is None:
                # python is not being unlinked
                log.debug("found in current prefix python version %s", linked_python_version)
                return linked_python_version

        # there won't be any python in the finished environment
        log.debug("no python version found in prefix")
        return None

    @staticmethod
    def _make_link_actions(transaction_context, package_info, target_prefix, requested_link_type,
                           requested_spec):
        required_quad = transaction_context, package_info, target_prefix, requested_link_type

        file_link_actions = LinkPathAction.create_file_link_actions(*required_quad)
        create_directory_actions = LinkPathAction.create_directory_actions(
            *required_quad, file_link_actions=file_link_actions
        )
        create_nonadmin_actions = CreateNonadminAction.create_actions(*required_quad)
        create_menu_actions = MakeMenuAction.create_actions(*required_quad)

        # if requested_spec:
        #     application_entry_point_actions = CreateApplicationEntryPointAction.create_actions(
        #         *required_quad
        #     )
        #     application_softlink_actions = CreateApplicationSoftlinkAction.create_actions(
        #         *required_quad
        #     )
        # else:
        #     application_entry_point_actions = ()
        #     application_softlink_actions = ()
        # leased_paths = tuple(axn.leased_path_entry for axn in concatv(
        #     application_entry_point_actions,
        #     application_softlink_actions,
        # ))

        # if requested_spec:
        #     register_private_env_actions = RegisterPrivateEnvAction.create_actions(
        #         transaction_context, package_info, target_prefix, requested_spec, leased_paths
        #     )
        # else:
        #     register_private_env_actions = ()

        # the ordering here is significant
        return tuple(concatv(
            create_directory_actions,
            file_link_actions,
            create_nonadmin_actions,
            create_menu_actions,
            # application_entry_point_actions,
            # register_private_env_actions,
        ))

    @staticmethod
    def _make_entry_point_actions(transaction_context, package_info, target_prefix,
                                  requested_link_type, requested_spec, link_action_groups):
        required_quad = transaction_context, package_info, target_prefix, requested_link_type
        return CreatePythonEntryPointAction.create_actions(*required_quad)

    @staticmethod
    def _make_compile_actions(transaction_context, package_info, target_prefix,
                              requested_link_type, requested_spec, link_action_groups):
        required_quad = transaction_context, package_info, target_prefix, requested_link_type
        link_action_group = next(ag for ag in link_action_groups if ag.pkg_data == package_info)
        return CompileMultiPycAction.create_actions(*required_quad,
                                                    file_link_actions=link_action_group.actions)

    def _make_legacy_action_groups(self):
        # this code reverts json output for plan back to previous behavior
        #   relied on by Anaconda Navigator and nb_conda
        legacy_action_groups = []

        if self._pfe is None:
            self._get_pfe()

        for q, (prefix, setup) in enumerate(iteritems(self.prefix_setups)):
            actions = defaultdict(list)
            if q == 0:
                self._pfe.prepare()
                download_urls = set(axn.url for axn in self._pfe.cache_actions)
                actions['FETCH'].extend(prec for prec in self._pfe.link_precs
                                        if prec.url in download_urls)

            actions['PREFIX'] = setup.target_prefix
            for prec in setup.unlink_precs:
                actions['UNLINK'].append(prec)
            for prec in setup.link_precs:
                actions['LINK'].append(prec)

            legacy_action_groups.append(actions)

        return legacy_action_groups

    def print_transaction_summary(self):
        legacy_action_groups = self._make_legacy_action_groups()

        download_urls = set(axn.url for axn in self._pfe.cache_actions)

        for actions, (prefix, stp) in zip(legacy_action_groups, iteritems(self.prefix_setups)):
            change_report = self._calculate_change_report(prefix, stp.unlink_precs, stp.link_precs,
                                                          download_urls, stp.remove_specs,
                                                          stp.update_specs)
            change_report_str = self._change_report_str(change_report)
            print(ensure_text_type(change_report_str))

        return legacy_action_groups

    def _change_report_str(self, change_report):
        builder = ['', '## Package Plan ##\n']
        builder.append('  environment location: %s' % change_report.prefix)
        builder.append('')
        if change_report.specs_to_remove:
            builder.append('  removed specs:%s'
                           % dashlist(sorted(text_type(s) for s in change_report.specs_to_remove),
                                      indent=4))
            builder.append('')
        if change_report.specs_to_add:
            builder.append('  added / updated specs:%s'
                           % dashlist(sorted(text_type(s) for s in change_report.specs_to_add),
                                      indent=4))
            builder.append('')

        def channel_filt(s):
            if context.show_channel_urls is False:
                return ''
            if context.show_channel_urls is None and s == DEFAULTS_CHANNEL_NAME:
                return ''
            return s

        def print_dists(dists_extras):
            lines = []
            fmt = "    %-27s|%17s"
            lines.append(fmt % ('package', 'build'))
            lines.append(fmt % ('-' * 27, '-' * 17))
            for prec, extra in dists_extras:
                line = fmt % (strip_global(prec.namekey) + '-' + prec.version, prec.build)
                if extra:
                    line += extra
                lines.append(line)
            return lines

        convert_namekey = lambda x: ("0:" + x[7:]) if x.startswith("global:") else x
        strip_global = lambda x: x[7:] if x.startswith("global:") else x

        if change_report.fetch_precs:
            builder.append("\nThe following packages will be downloaded:\n")

            disp_lst = []
            total_download_bytes = 0
            for prec in sorted(change_report.fetch_precs,
                               key=lambda x: convert_namekey(x.namekey)):
                size = prec.size
                extra = '%15s' % human_bytes(size)
                total_download_bytes += size
                schannel = channel_filt(text_type(prec.channel.canonical_name))
                if schannel:
                    extra += '  ' + schannel
                disp_lst.append((prec, extra))
            builder.extend(print_dists(disp_lst))

            builder.append(' ' * 4 + '-' * 60)
            builder.append(" " * 43 + "Total: %14s" % human_bytes(total_download_bytes))

        def diff_strs(unlink_prec, link_prec):
            channel_change = unlink_prec.channel.name != link_prec.channel.name
            subdir_change = unlink_prec.subdir != link_prec.subdir
            version_change = unlink_prec.version != link_prec.version
            build_change = unlink_prec.build != link_prec.build

            builder_left = []
            builder_right = []

            if channel_change or subdir_change:
                if unlink_prec.channel.name is not None:
                    builder_left.append(unlink_prec.channel.name)
                if link_prec.channel.name is not None:
                    builder_right.append(link_prec.channel.name)
            if subdir_change:
                builder_left.append("/" + unlink_prec.subdir)
                builder_right.append("/" + link_prec.subdir)
            if (channel_change or subdir_change) and (version_change or build_change):
                builder_left.append("::" + unlink_prec.name + "-")
                builder_right.append("::" + link_prec.name + "-")
            if version_change or build_change:
                builder_left.append(unlink_prec.version + "-" + unlink_prec.build)
                builder_right.append(link_prec.version + "-" + link_prec.build)

            return ''.join(builder_left), ''.join(builder_right)

        def add_single(display_key, disp_str):
            if len(display_key) > 18:
                display_key = display_key[:17] + "~"
            builder.append("  %-18s %s" % (display_key, disp_str))

        def add_double(display_key, left_str, right_str):
            if len(display_key) > 18:
                display_key = display_key[:17] + "~"
            if len(left_str) > 38:
                left_str = left_str[:37] + "~"
            builder.append("  %-18s %38s --> %s" % (display_key, left_str, right_str))

        if change_report.new_precs:
            builder.append("\nThe following NEW packages will be INSTALLED:\n")
            for namekey in sorted(change_report.new_precs, key=convert_namekey):
                link_prec = change_report.new_precs[namekey]
                display_key = strip_global(namekey)
                add_single(display_key, link_prec.record_id())

        if change_report.removed_precs:
            builder.append("\nThe following packages will be REMOVED:\n")
            for namekey in sorted(change_report.removed_precs, key=convert_namekey):
                unlink_prec = change_report.removed_precs[namekey]
                builder.append("  " + "-".join(
                    (unlink_prec.name, unlink_prec.version, unlink_prec.build)
                ))

        if change_report.updated_precs:
            builder.append("\nThe following packages will be UPDATED:\n")
            for namekey in sorted(change_report.updated_precs, key=convert_namekey):
                unlink_prec, link_prec = change_report.updated_precs[namekey]
                display_key = strip_global(namekey)
                left_str, right_str = diff_strs(unlink_prec, link_prec)
                add_double(display_key, left_str, right_str)

        if change_report.superseded_precs:
            builder.append("\nThe following packages will be SUPERSEDED "
                           "by a higher-priority channel:\n")
            for namekey in sorted(change_report.superseded_precs, key=convert_namekey):
                unlink_prec, link_prec = change_report.superseded_precs[namekey]
                display_key = strip_global(namekey)
                left_str, right_str = diff_strs(unlink_prec, link_prec)
                add_double(display_key, left_str, right_str)

        if change_report.downgraded_precs:
            builder.append("\nThe following packages will be DOWNGRADED:\n")
            for namekey in sorted(change_report.downgraded_precs, key=convert_namekey):
                unlink_prec, link_prec = change_report.downgraded_precs[namekey]
                display_key = strip_global(namekey)
                left_str, right_str = diff_strs(unlink_prec, link_prec)
                add_double(display_key, left_str, right_str)
        builder.append('')
        builder.append('')
        return "\n".join(builder)

    @staticmethod
    def _calculate_change_report(prefix, unlink_precs, link_precs, download_urls, specs_to_remove,
                                 specs_to_add):
        unlink_map = {prec.namekey: prec for prec in unlink_precs}
        link_map = {prec.namekey: prec for prec in link_precs}
        unlink_namekeys, link_namekeys = set(unlink_map), set(link_map)

        removed_precs = {namekey: unlink_map[namekey]
                         for namekey in (unlink_namekeys - link_namekeys)}
        new_precs = {namekey: link_map[namekey]
                     for namekey in (link_namekeys - unlink_namekeys)}

        # updated means a version increase, or a build number increase
        # downgraded means a version decrease, or build number decrease, but channel canonical_name
        #   has to be the same
        # superseded then should be everything else left over
        updated_precs = {}
        downgraded_precs = {}
        superseded_precs = {}

        common_namekeys = link_namekeys & unlink_namekeys
        for namekey in common_namekeys:
            unlink_prec, link_prec = unlink_map[namekey], link_map[namekey]
            unlink_vo = VersionOrder(unlink_prec.version)
            link_vo = VersionOrder(link_prec.version)
            build_number_increases = link_prec.build_number > unlink_prec.build_number
            if link_vo == unlink_vo and build_number_increases or link_vo > unlink_vo:
                updated_precs[namekey] = (unlink_prec, link_prec)
            elif (link_prec.channel.name == unlink_prec.channel.name
                    and link_prec.subdir == unlink_prec.subdir):
                if link_prec == unlink_prec:
                    # noarch: python packages are re-linked on a python version change
                    # just leave them out of the package report
                    continue
                downgraded_precs[namekey] = (unlink_prec, link_prec)
            else:
                superseded_precs[namekey] = (unlink_prec, link_prec)

        fetch_precs = set(prec for prec in link_precs if prec.url in download_urls)
        change_report = ChangeReport(prefix, specs_to_remove, specs_to_add, removed_precs,
                                     new_precs, updated_precs, downgraded_precs, superseded_precs,
                                     fetch_precs)
        return change_report


def run_script(prefix, prec, action='post-link', env_prefix=None, activate=False):
    """
    call the post-link (or pre-unlink) script, and return True on success,
    False on failure
    """
    path = join(prefix,
                'Scripts' if on_win else 'bin',
                '.%s-%s.%s' % (prec.name, action, 'bat' if on_win else 'sh'))
    if not isfile(path):
        return True

    env = os.environ.copy()

    if action == 'pre-link':  # pragma: no cover
        # old no-arch support; deprecated
        is_old_noarch = False
        try:
            with open(path) as f:
                script_text = ensure_text_type(f.read())
            if ((on_win and "%PREFIX%\\python.exe %SOURCE_DIR%\\link.py" in script_text)
                    or "$PREFIX/bin/python $SOURCE_DIR/link.py" in script_text):
                is_old_noarch = True
        except Exception as e:
            log.debug(e, exc_info=True)

        env['SOURCE_DIR'] = prefix
        if not is_old_noarch:
            warnings.warn(dals("""
            Package %s uses a pre-link script. Pre-link scripts are potentially dangerous.
            This is because pre-link scripts have the ability to change the package contents in the
            package cache, and therefore modify the underlying files for already-created conda
            environments.  Future versions of conda may deprecate and ignore pre-link scripts.
            """) % prec.dist_str())

    script_caller = None
    if on_win:
        ensure_comspec_set()
        try:
            comspec = os.environ[str('COMSPEC')]
        except KeyError:
            log.info("failed to run %s for %s due to COMSPEC KeyError", action, prec.dist_str())
            return False
        if activate:
            script_caller, command_args = wrap_subprocess_call(
                on_win, context.root_prefix, prefix, context.dev, False, ('@CALL', path)
            )
        else:
            command_args = [comspec, '/d', '/c', path]
    else:
        shell_path = 'sh' if 'bsd' in sys.platform else 'bash'
        if activate:
            script_caller, command_args = wrap_subprocess_call(
                on_win, context.root_prefix, prefix, context.dev, False, (".", path)
            )
        else:
            shell_path = 'sh' if 'bsd' in sys.platform else 'bash'
            command_args = [shell_path, "-x", path]

    env['ROOT_PREFIX'] = context.root_prefix
    env['PREFIX'] = env_prefix or prefix
    env['PKG_NAME'] = prec.name
    env['PKG_VERSION'] = prec.version
    env['PKG_BUILDNUM'] = prec.build_number
    env['PATH'] = os.pathsep.join((dirname(path), env.get('PATH', '')))

    log.debug("for %s at %s, executing script: $ %s",
              prec.dist_str(), env['PREFIX'], ' '.join(command_args))
    try:
        response = subprocess_call(command_args, env=env, path=dirname(path), raise_on_error=False)
        if response.rc != 0:
            m = messages(prefix)
            if action in ('pre-link', 'post-link'):
                if 'openssl' in prec.dist_str():
                    # this is a hack for conda-build string parsing in the conda_build/build.py
                    #   create_env function
                    message = "%s failed for: %s" % (action, prec)
                else:
                    message = dals("""
                    %s script failed for package %s
                    location of failed script: %s
                    ==> script messages <==
                    %s
                    ==> script output <==
                    stdout: %s
                    stderr: %s
                    return code: %s
                    """) % (action, prec.dist_str(), path, m or "<None>",
                            response.stdout, response.stderr, response.rc)
                raise LinkError(message)
            else:
                log.warn("%s script failed for package %s\n"
                         "consider notifying the package maintainer", action, prec.dist_str())
                return False
        else:
            messages(prefix)
            return True
    finally:
        if script_caller is not None:
            if 'CONDA_TEST_SAVE_TEMPS' not in os.environ:
                rm_rf(script_caller)
            else:
                log.warning('CONDA_TEST_SAVE_TEMPS :: retaining run_script {}'.format(
                    script_caller))


def messages(prefix):
    path = join(prefix, '.messages.txt')
    try:
        if isfile(path):
            with open(path) as fi:
                m = fi.read()
                if hasattr(m, "decode"):
                    m = m.decode('utf-8')
                print(m.encode('utf-8'), file=sys.stderr if context.json else sys.stdout)
                return m
    finally:
        rm_rf(path)
