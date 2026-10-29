# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import fnmatch
import os
import re
import shutil
from glob import glob
import tempfile

import gluetool
from gluetool.glue import GlueError
from gluetool.log import log_dict
from gluetool.utils import Command, normalize_bool_option, render_template
from gluetool.result import Result
from gluetool_modules_framework.libs.threading import RepeatTimer
from gluetool_modules_framework.libs.pipeline_stages import PIPELINE_ARCHIVING_STARTED, PIPELINE_ARCHIVING_FINISHED

from typing import Any, Dict, List, Optional, Tuple

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10
DEFAULT_PARALLEL_ARCHIVING_TICK = 30
DEFAULT_RSYNC_TIMEOUT = 120
DEFAULT_VERIFY_TICK = 5
DEFAULT_VERIFY_TIMEOUT = 1800
DEFAULT_PARALLEL_ARCHIVING_FINISH_TICK = 5
DEFAULT_PARALLEL_ARCHIVING_FINISH_TIMEOUT = 1800
DEFAULT_BATCH_MAX_FILES = 0
DEFAULT_BATCH_MAX_SIZE = 256

ARCHIVE_STAGES = ['execute', 'progress', 'destroy']
# Stages which use a copy for syncing
ARCHIVE_STAGES_USING_COPY = ['execute', 'progress']
SOURCE_DESTINATION_ENTRY_KEYS = ['source', 'exclude', 'destination', 'permissions', 'verify']

# Layout of a batch staging directory, see `prepare_batch_tree`. The file list deliberately lives
# next to the transfer root, never inside it - `hide_secrets` runs `sed -i` over everything under
# its search path, so a secret value appearing in a path would otherwise rewrite the list itself.
BATCH_TREE_DIR_NAME = 'tree'
BATCH_LIST_FILE_NAME = 'files-from.txt'
# Scratch directory of `rsync_filter_names`, also next to the transfer root.
BATCH_FILTER_DIR_NAME = 'filter'

# rsync options adding filter rules, see `rsync_filter_options`. The first group takes a value, either
# as `--option=value` or as the next argument, the second one does not.
RSYNC_FILTER_VALUE_OPTIONS = ['--exclude', '--include', '--filter', '--exclude-from', '--include-from', '-f']
RSYNC_FILTER_FLAG_OPTIONS = ['--cvs-exclude', '-C', '-F', '-FF']

# rsync exit codes tolerated for a batched transfer:
# * 24 - source files vanished, expected against logs which are still being written to
# * 23 - partial transfer due to an error, a single unreadable file must not cost the whole batch
RSYNC_VANISHED_EXIT_CODE = 24
RSYNC_PARTIAL_EXIT_CODE = 23
RSYNC_PARTIAL_EXIT_CODES = [RSYNC_PARTIAL_EXIT_CODE, RSYNC_VANISHED_EXIT_CODE]


class Archive(gluetool.Module):
    """
    The module is used to archive artifacts from the testing farm to a remote location.

    The module supports SOURCE_DESTINATION_MAP environment variable which can be used to specify
    additional source-destination mappings. The variable is a string of the following format:
    <SOURCE>:[<EXCLUDE1>,<EXCLUDE2>]:[<DESTINATION>]:[<PERMISSIONS>]:[<STAGE>]#...

    For example:
    example-directory/*:^excluded-file1$,^excluded-file2$:example-destination:777:destroy

    The ``SOURCE`` is a path to the directory that should be archived. It is a glob.
    The ``EXCLUDE`` is a list of patterns used to exclude files from the archive. The entries are regexes.
    The ``DESTINATION`` is a path to the destination directory.
    The ``PERMISSIONS`` defines the permissions for the destination directory.
    The ``STAGE`` specifies when the archive should be performed. See ARCHIVE_STAGES for available stages.

    The ``archive-mode`` option supports four modes: ``daemon``, ``ssh``, ``local`` and ``s3``:
    * The ``daemon`` mode uses the rsync daemon to with rsync protocol
    * The ``ssh`` mode uses rsync with ssh protocol
    * The ``local`` mode copies files locally with rsync. It should be used only for testing and development.
    * The ``s3`` mode uses the AWS cli to sync files to S3 bucket.

    Multiple modes can be selected with a comma-separated list or by repeating ``archive-mode``.
    For example, ``--archive-mode daemon,s3`` archives artifacts with both rsync and S3.

    Use the ``verify`` flag to verify given path on the artifact location provided by the ``coldstore`` module.
    Verification checks only that location, not every selected archive mode, and is skipped if any mode fails
    or only ``local`` mode is selected.

    The ``enable-batched-archiving`` option makes the copy based stages transfer all their files with a
    single ``rsync --files-from`` invocation per rsync archive mode instead of one invocation per file.
    A stage globbing hundreds of files therefore opens one connection to the artifacts host instead of
    hundreds, and runs ``hide_secrets`` once over the staged tree instead of once per file. The ``s3``
    mode, if selected, uploads the same staged and redacted files one by one.

    Files are batched only when nothing about them needs a whole invocation rsync option or a per file
    step of its own, see :py:meth:`is_batchable`. Anything else, and the whole ``destroy`` stage, keeps
    using the per file code path, which is also the fallback when a batched transfer fails.

    Filter rules passed via ``rsync-options``, like ``--exclude``, keep their per file behaviour. A per
    file transfer of a copy based stage shows rsync only the file name, while a batched one shows the
    whole relative path, so the batched invocation leaves the rules out and the module evaluates them
    against the file names instead, see :py:meth:`rsync_filter_names`. Use the per entry ``exclude``
    regexes to keep files out of the archive by their path, and ``batch-exclude`` to archive files one
    by one instead of in a batch.
    """

    name = 'archive'
    description = 'Archive artifacts from the testing farm to a remote location.'

    options = {
        'disable-archiving': {
            'help': 'Disable archiving.',
            'action': 'store_true',
        },
        'artifacts-host': {
            'help': 'Host of the machine where artifacts will be stored. It can also contain user i.e. user@host.',
            'type': str,
        },
        'artifacts-rsync-host': {
            'help': 'Rsync daemon host where artifacts will be stored.',
            'type': str,
        },
        'artifacts-root': {
            'help': 'Root directory where artifacts will be stored.',
            'type': str,
        },
        'artifacts-local-root': {
            'help': 'Root directory where artifacts will be stored locally.',
            'type': str,
        },
        'source-destination-map': {
            'help': 'Mapping of source to destination paths and permissions.',
            'metavar': 'FILE'
        },
        'archive-mode': {
            'help': 'Archive modes to use (daemon, ssh, local, s3). Can be specified multiple times or as a '
                    'comma-separated list. Artifacts are archived using every selected mode.',
            'action': 'append',
            'default': [],
        },
        'rsync-options': {
            'help': 'Rsync options to use.',
            'action': 'append',
            'default': []
        },
        'retry-tick': {
            'help': 'Timeout between retries for failed rsync operations. (default: %(default)s)',
            'metavar': 'RETRY_TICK',
            'type': int,
            'default': DEFAULT_RETRY_TICK,
        },
        'retry-timeout': {
            'help': 'Timeout for rsync retries in seconds. (default: %(default)s)',
            'metavar': 'RETRY_TIMEOUT',
            'type': int,
            'default': DEFAULT_RETRY_TIMEOUT,
        },
        'verify-tick': {
            'help': 'Timeout between archive verification retries. (default: %(default)s)',
            'metavar': 'VERIFY_TICK',
            'type': int,
            'default': DEFAULT_VERIFY_TICK,
        },
        'verify-timeout': {
            'help': 'Timeout for archive verification in seconds. (default: %(default)s)',
            'metavar': 'VERIFY_TIMEOUT',
            'type': int,
            'default': DEFAULT_VERIFY_TIMEOUT,
        },
        'rsync-timeout': {
            'help': 'Timeout for the rsync command. (default: %(default)s)',
            'metavar': 'RCYNC_TIMEOUT',
            'type': int,
            'default': DEFAULT_RSYNC_TIMEOUT,
        },
        'enable-parallel-archiving': {
            'help': 'Enable archiving which runs in parallel with the pipeline execution.',
            'action': 'store_true',
        },
        'parallel-archiving-tick': {
            'help': 'Archive artifacts every PIPELINE_CANCELLATION_TICK seconds (default: %(default)s)',
            'metavar': 'PARALLEL_ARCHIVING_TICK',
            'type': int,
            'default': DEFAULT_PARALLEL_ARCHIVING_TICK
        },
        'parallel-archiving-finish-timeout': {
            'help': 'Timeout for parallel archiving to finish in seconds. (default: %(default)s)',
            'metavar': 'PARALLEL_ARCHIVING_FINISH_TIMEOUT',
            'type': int,
            'default': DEFAULT_PARALLEL_ARCHIVING_FINISH_TIMEOUT,
        },
        'parallel-archiving-finish-tick': {
            'help': 'Timeout between parallel archiving finish checks. (default: %(default)s)',
            'metavar': 'PARALLEL_ARCHIVING_FINISH_TICK',
            'type': int,
            'default': DEFAULT_PARALLEL_ARCHIVING_FINISH_TICK,
        },
        'enable-batched-archiving': {
            'help': """
                    Transfer the copy based stages ({}) with a single batched rsync invocation per stage
                    and rsync archive mode instead of one invocation per file. (default: %(default)s)
                    """.format(', '.join(ARCHIVE_STAGES_USING_COPY)),
            'metavar': 'yes|no',
            'default': 'no',
        },
        'batch-max-files': {
            'help': 'Maximum number of files in a single batched rsync invocation, 0 means unlimited. '
                    '(default: %(default)s)',
            'metavar': 'BATCH_MAX_FILES',
            'type': int,
            'default': DEFAULT_BATCH_MAX_FILES,
        },
        'batch-max-size': {
            'help': """
                    Maximum total size, in MiB, of the files staged for a single batched rsync invocation,
                    0 means unlimited. A batch keeps a copy of all its files in the temporary directory at
                    once, this bounds how much space that takes. (default: %(default)s)
                    """,
            'metavar': 'MIB',
            'type': int,
            'default': DEFAULT_BATCH_MAX_SIZE,
        },
        'batch-exclude': {
            'help': """
                    Glob patterns, matched against the transfer relative path, of files to archive one by
                    one instead of in a batch. (default: none)
                    """,
            'action': 'append',
            'default': [],
        },
        'aws-region': {
            'help': 'AWS region to use for S3 archiving.',
            'type': str,
        },
        'aws-s3-bucket': {
            'help': 'AWS S3 bucket to use for archiving.',
            'type': str,
        },
        'aws-access-key-id': {
            'help': 'AWS access key ID to use for archiving.',
            'type': str,
        },
        'aws-secret-access-key': {
            'help': 'AWS secret access key to use for archiving.',
            'type': str,
        },
        'aws-options': {
            'help': 'AWS cli options to use.',
            'action': 'append',
            'default': []
        },
    }

    required_options = ('source-destination-map', 'artifacts-root', 'archive-mode',)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(Archive, self).__init__(*args, **kwargs)

        self._archive_timer: Optional[RepeatTimer] = None
        self._request_id: Optional[str] = None
        # List of (archive mode, path) tuples for created directories.
        # We need to keep track of them to avoid creating them multiple times.
        self._created_directories: List[Tuple[str, str]] = []

    @property
    def archive_modes(self) -> List[str]:
        return list(dict.fromkeys(gluetool.utils.normalize_multistring_option(self.option('archive-mode'))))

    @property
    def rsync_archive_modes(self) -> List[str]:
        return [mode for mode in self.archive_modes if mode != 's3']

    def sanity(self) -> None:
        modes = self.archive_modes
        invalid = [mode for mode in modes if mode not in ('daemon', 'ssh', 'local', 's3')]
        if not modes or invalid:
            raise GlueError('invalid archive modes: {}'.format(', '.join(invalid) or '(none)'))

        if 'daemon' in self.archive_modes and not self.option('artifacts-rsync-host'):
            raise GlueError('rsync daemon host must be specified when using rsync daemon mode')

        if 'ssh' in self.archive_modes and not self.option('artifacts-host'):
            raise GlueError('artifacts host must be specified when using ssh mode')

        if 'local' in self.archive_modes and not self.option('artifacts-local-root'):
            raise GlueError('artifacts local root must be specified when using local mode')

        if 's3' in self.archive_modes:
            for option in ['aws-region', 'aws-s3-bucket', 'aws-access-key-id', 'aws-secret-access-key']:
                if not self.option(option):
                    raise GlueError('{} option must be specified when using s3 mode'.format(option))

    def source_destination_map(self) -> Any:
        source_destination_map = gluetool.utils.load_yaml(
            gluetool.utils.normalize_path(self.option('source-destination-map')),
        )

        additional_map_var = os.environ.get('SOURCE_DESTINATION_MAP', None)
        if additional_map_var:
            for entry in additional_map_var.split('#'):

                source, exclude, destination, permissions, stage = entry.split(':')
                exclude_split = [entry for entry in exclude.split(',') if entry]

                if stage not in ARCHIVE_STAGES:
                    raise GlueError('Invalid stage "{}" in SOURCE_DESTINATION_MAP env'.format(stage))

                source_destination_map[stage].append({
                    'source': source,
                    'exclude': exclude_split or [],
                    'destination': destination or '',
                    'permissions': permissions or None
                })

        # Render all templates in the map
        # and check everything is set correct
        context = self.shared('eval_context')

        for stage_name, map_stage in source_destination_map.items():
            if stage_name not in ARCHIVE_STAGES:
                raise GlueError('Invalid stage "{}" in source-destination-map'.format(stage_name))

            for dict_entry in map_stage:
                if dict_entry.get('source') is None:
                    raise GlueError('Source path must be specified in source-destination-map entry')

                for key, value in dict_entry.items():
                    if key not in SOURCE_DESTINATION_ENTRY_KEYS:
                        raise GlueError('Invalid key "{}" in source-destination-map entry'.format(key))

                    # Render possible templates in the value
                    if value is not None:
                        # Exclude value is a list of strings
                        if isinstance(value, list):
                            dict_entry[key] = [
                                render_template(
                                    sub_entry,
                                    logger=self.logger,
                                    **context
                                )
                                for sub_entry in value
                            ]
                        else:
                            dict_entry[key] = render_template(
                                value, logger=self.logger, **context
                            )

        return source_destination_map

    @gluetool.utils.cached_property
    def artifacts_host(self) -> str:
        return render_template(
            self.option('artifacts-host'),
            logger=self.logger,
            **self.shared('eval_context')
        )

    @gluetool.utils.cached_property
    def artifacts_rsync_host(self) -> str:
        return render_template(
            self.option('artifacts-rsync-host'),
            logger=self.logger,
            **self.shared('eval_context')
        )

    @gluetool.utils.cached_property
    def artifacts_root(self) -> str:
        return render_template(
            self.option('artifacts-root'),
            logger=self.logger,
            **self.shared('eval_context')
        )

    @gluetool.utils.cached_property
    def artifacts_local_root(self) -> str:
        return render_template(
            self.option('artifacts-local-root'),
            logger=self.logger,
            **self.shared('eval_context')
        )

    @gluetool.utils.cached_property
    def rsync_options(self) -> List[str]:

        options = gluetool.utils.normalize_multistring_option(self.option('rsync-options'))

        rendered_options = [
            render_template(option, logger=self.logger, **self.shared('eval_context'))
            for option in options
        ]

        rendered_options.append('--timeout={}'.format(self.option('rsync-timeout')))

        return rendered_options

    @gluetool.utils.cached_property
    def batched_archiving(self) -> bool:
        """
        Whether the copy based stages transfer their files with a single batched rsync invocation.

        Read through ``normalize_bool_option`` on purpose, rather than declaring the option as
        ``store_true``. This is a revert switch for a hot code path, and ``enable-... = no`` in a
        configuration file must disable it - with ``store_true`` the option value would be the
        non-empty, and therefore truthy, string ``'no'``.
        """

        return normalize_bool_option(self.option('enable-batched-archiving'))

    @gluetool.utils.cached_property
    def batch_excludes(self) -> List[str]:
        return gluetool.utils.normalize_multistring_option(self.option('batch-exclude'))

    @gluetool.utils.cached_property
    def _rsync_option_groups(self) -> Tuple[List[str], List[str]]:
        """
        Split ``rsync-options`` into the options adding filter rules and all the others, both in their
        original order.

        Bundled short options, like ``-aC``, are not recognized and stay with the others.
        """

        filter_options: List[str] = []
        other_options: List[str] = []
        options = iter(self.rsync_options)

        for option in options:
            if option in RSYNC_FILTER_FLAG_OPTIONS:
                filter_options.append(option)

            elif option in RSYNC_FILTER_VALUE_OPTIONS:
                filter_options += [option, next(options, '')]

            elif any(option.startswith('{}='.format(name)) for name in RSYNC_FILTER_VALUE_OPTIONS):
                filter_options.append(option)

            elif option.startswith('-f') and not option.startswith('--'):
                filter_options.append(option)

            else:
                other_options.append(option)

        return filter_options, other_options

    @property
    def rsync_filter_options(self) -> List[str]:
        """
        The ``rsync-options`` which add filter rules. :py:meth:`rsync_filter_names` applies them to
        batched files.
        """

        return self._rsync_option_groups[0]

    @property
    def rsync_transfer_options(self) -> List[str]:
        """
        The ``rsync-options`` of a batched transfer, without any filter rules.

        A batched transfer matches filter rules against the whole relative path, while a per file one
        matches them against the file name. :py:meth:`rsync_filter_names` has applied the rules to the
        file names already, so the transfer leaves them out entirely. Keeping them and neutralizing them
        with a leading ``--include=*`` would not do: a rule like ``--filter=!`` clears the list,
        including that include.
        """

        return self._rsync_option_groups[1]

    @gluetool.utils.cached_property
    def aws_options(self) -> List[str]:

        options = gluetool.utils.normalize_multistring_option(self.option('aws-options'))

        rendered_options = [
            render_template(option, logger=self.logger, **self.shared('eval_context'))
            for option in options
        ]

        return rendered_options

    def create_archive_directory_ssh(self, directory: Optional[str] = None) -> None:
        """
        Creates directory on the host with ssh where artifacts will be stored.
        If directory is not set, it will create the root directory.
        """
        assert self._request_id is not None

        path = os.path.join(self.artifacts_root, self._request_id)
        if directory:
            path = os.path.join(path, directory)

        if ('ssh', path) in self._created_directories:
            return

        cmd: List[str] = [
            'ssh',
            self.artifacts_host,
            'mkdir',
            '-p',
            path
        ]

        def _run_ssh() -> Result[bool, bool]:
            try:
                Command(cmd, logger=self.logger).run()
            except gluetool.GlueCommandError as exc:
                self.warn('ssh command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)
            return Result.Ok(True)

        gluetool.utils.wait(
            "creating '{}' directory with ssh".format(path),
            _run_ssh,
            timeout=self.option('retry-timeout'),
            tick=self.option('retry-tick')
        )

        self._created_directories.append(('ssh', path))

    def create_archive_directory_rsync(self, directory: Optional[str] = None) -> None:
        """
        Creates directory on the host with rsync where artifacts will be stored.
        If directory is not set, it will create the root directory.
        """
        assert self._request_id is not None

        path = self._request_id

        if directory:
            path = os.path.join(path, directory)

        if ('daemon', path) in self._created_directories:
            return

        cmd = ['rsync']

        cmd += self.rsync_options

        cmd.append('--mkpath')

        cmd.append('/dev/null')

        cmd.append('rsync://{}/{}/'.format(
                self.artifacts_rsync_host,
                path
        ))

        def _run_rsync() -> Result[bool, bool]:
            try:
                Command(cmd, logger=self.logger).run()
            except gluetool.GlueCommandError as exc:
                self.warn('rsync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)
            return Result.Ok(True)

        gluetool.utils.wait(
            "creating '{}' directory with rsync".format(path),
            _run_rsync,
            timeout=self.option('retry-timeout'),
            tick=self.option('retry-tick')
        )

        self._created_directories.append(('daemon', path))

    def create_archive_directory_local(self, directory: Optional[str] = None) -> None:
        """
        Creates directory on the local host.
        """
        assert self._request_id is not None

        path = os.path.join(self.artifacts_local_root, self._request_id)

        if directory:
            path = os.path.join(path, directory)

        if ('local', path) in self._created_directories:
            return

        cmd = [
            'mkdir',
            '-p',
            path
        ]

        Command(cmd, logger=self.logger).run()

        self._created_directories.append(('local', path))

    def prepare_source_copy(self, original_source: str) -> Tuple[str, str]:
        original_source = original_source.rstrip('/')

        # Create a copy in the temporary directory with
        source = os.path.join(tempfile.mkdtemp(), os.path.basename(original_source))
        if os.path.isdir(original_source):
            shutil.copytree(
                original_source, source,
                # preserve symlinks
                symlinks=True,
                # ignore dangling symlinks, if they would exist
                ignore_dangling_symlinks=True,
                # this should not be needed, but rather setting it to mitigate certain corner cases
                dirs_exist_ok=True
            )
        else:
            shutil.copy2(original_source, source, follow_symlinks=False)

        return source, original_source

    def delete_source_copy(self, source: str) -> None:
        self.debug('removing source copy {}'.format(source))
        # Delete the temporary created directory
        shutil.rmtree(os.path.dirname(source))

    def _archiving_cancelled(self, stage: str) -> bool:
        """
        Whether parallel archiving has been cancelled and the ``progress`` stage should stop.
        """

        return bool(stage == 'progress' and self._archive_timer and self._archive_timer.finished.is_set())

    def batch_relpath(self, source: str) -> str:
        """
        Transfer relative path of ``source``.

        This is deliberately identical to the destination :py:meth:`run_rsync` derives for a source
        copy, where ``destination`` is overwritten with the original source. Together with the
        ``--relative`` of a batched transfer it makes the file land at exactly the same remote path as
        a per file one.
        """

        return os.path.normpath(source).lstrip('/')

    def is_batchable(self, stage: str, entry: Dict[str, Any], source: str) -> bool:
        """
        Whether a single glob match can travel inside a batched rsync invocation.

        The predicate is deliberately conservative. Anything which needs a whole invocation rsync
        option of its own, or a per file step after the transfer, stays on the per file code path.
        """

        if not self.batched_archiving:
            return False

        # Batching saves rsync invocations, without an rsync archive mode there are none to save.
        if not self.rsync_archive_modes:
            return False

        # Only the copy based stages ignore the configured destination and derive it from the source,
        # which is what makes a batched transfer destination identical to a per file one. The
        # `destroy` stage also carries the upload ordering contract and recursive directory syncs.
        if stage not in ARCHIVE_STAGES_USING_COPY:
            return False

        # Verification polls the artifacts location for one particular file after its transfer.
        if normalize_bool_option(entry.get('verify') or False):
            return False

        # `--chmod` applies to the whole invocation, including the directories implied by
        # `--relative`, so batching an entry with permissions would also chmod its parents.
        if entry.get('permissions'):
            return False

        # `--recursive` applies to the whole invocation as well.
        if os.path.isdir(source):
            return False

        # A parent relative source, like `../outside.log`, would be staged outside of the batch tree.
        relpath = self.batch_relpath(source)
        if relpath == os.pardir or relpath.startswith(os.pardir + os.sep):
            return False

        if any(fnmatch.fnmatch(relpath, pattern) for pattern in self.batch_excludes):
            self.debug('archiving {} on its own because it matches a batch-exclude pattern'.format(relpath))
            return False

        return True

    def rsync_filter_names(self, filter_root: str, names: List[str]) -> List[str]:
        """
        Evaluate the ``rsync-options`` filter rules against file names, the way a per file transfer
        does.

        A per file transfer of a source copy shows rsync only the file name, so a rule like
        ``work-*/**/discover/**`` never matches there. A ``--files-from`` transfer matches the rules
        against the whole relative path instead. To keep the per file result, a local dry run asks
        rsync itself which of the names it would transfer, with the very same rules.

        ``filter_root`` must not exist yet, it is created and filled with empty files named
        ``names``.

        :returns: names rsync would transfer.
        """

        source_root = os.path.join(filter_root, 'source')
        os.makedirs(source_root)

        for name in names:
            open(os.path.join(source_root, name), 'wb').close()

        list_file = self.write_batch_list(filter_root, names)

        cmd = ['rsync'] + self.rsync_filter_options + [
            '--dry-run',
            '--files-from={}'.format(list_file),
            '--from0',
            # Every name sits in the transfer root, as it does in a per file transfer.
            '--no-relative',
            '--out-format=%n',
            # Without this rsync escapes every non-ASCII byte as `\#ooo` under a non-UTF-8 locale, and
            # such a name would never match. Control characters stay escaped, see `rsync_filter_relpaths`.
            '--8-bit-output',
            source_root + os.sep,
            os.path.join(filter_root, 'destination') + os.sep,
        ]

        output = Command(cmd, logger=self.logger).run()

        transferred = set((output.stdout or '').splitlines())

        return [name for name in names if name in transferred]

    def rsync_filter_relpaths(self, batch_root: str, relpaths: List[str]) -> Tuple[List[str], List[str]]:
        """
        Apply the ``rsync-options`` filter rules to the file names of ``relpaths``, see
        :py:meth:`rsync_filter_names`.

        :returns: tuple of the relative paths a batched transfer carries, and those whose name the dry
            run cannot evaluate. The latter are transferred one by one, where rsync applies the rules
            itself. Relative paths the rules exclude are in neither.
        """

        if not self.rsync_filter_options:
            return relpaths, []

        evaluable: List[str] = []
        single: List[str] = []

        for relpath in relpaths:
            name = os.path.basename(relpath)

            # rsync escapes control characters in its `%n` output, such a name would never match.
            if name.isprintable() and '\\' not in name:
                evaluable.append(relpath)

            else:
                single.append(relpath)

        if not evaluable:
            return [], single

        kept_names = set(self.rsync_filter_names(
            os.path.join(batch_root, BATCH_FILTER_DIR_NAME),
            sorted(set(os.path.basename(relpath) for relpath in evaluable))
        ))

        kept: List[str] = []

        for relpath in evaluable:
            if os.path.basename(relpath) in kept_names:
                kept.append(relpath)

            else:
                self.debug('skipping {} because rsync-options filter rules exclude it'.format(relpath))

        return kept, single

    def prepare_batch_tree(
        self,
        batch_root: str,
        chunk: List[Tuple[str, Dict[str, Any]]]
    ) -> Tuple[str, Dict[str, Tuple[str, Dict[str, Any]]], List[Tuple[str, Dict[str, Any]]]]:
        """
        Stage copies of the ``chunk`` sources into a single temporary tree, preserving their relative
        paths.

        Replaces N :py:meth:`prepare_source_copy` calls, and therefore N temporary directories, with
        a single one. Sources which cannot be copied are dropped, a file disappearing between the
        glob and the copy is expected for logs which are still being written to.

        ``batch_root`` is created by the caller, so that a failure in here cannot leak it.

        :param chunk: pairs of a source and its map entry.
        :returns: tuple of the transfer root, the staged sources and their map entries keyed by their
            transfer relative path, and the sources the caller has to archive one by one instead.
        """

        tree_root = os.path.join(batch_root, BATCH_TREE_DIR_NAME)
        real_tree_root = os.path.realpath(tree_root)

        candidates: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        fallback: List[Tuple[str, Dict[str, Any]]] = []

        for source, entry in chunk:
            relpath = self.batch_relpath(source)

            if not relpath:
                continue

            # Sources sharing a transfer relative path land at the same remote path, like
            # `/tmp/example.log` and `tmp/example.log`. One by one, the source globbed last overwrites
            # the others, so it is the one to stage.
            candidates[relpath] = (source, entry)

        staged: Dict[str, Tuple[str, Dict[str, Any]]] = {}

        for relpath, (source, entry) in candidates.items():
            target = os.path.join(tree_root, relpath)

            # `is_batchable` keeps parent relative sources out, but a symlink staged earlier could still
            # redirect a later copy. `realpath` resolves whatever part of the target exists already.
            real_target = os.path.realpath(target)

            if os.path.commonpath([real_tree_root, real_target]) != real_tree_root:
                self.warn('{} would be staged outside of the batch tree, archiving it on its own'.format(source))
                fallback.append((source, entry))
                continue

            try:
                # `exist_ok` alone is not enough, it suppresses the error only when `os.path.isdir`
                # agrees the path is a directory, which also makes it racy against a concurrent
                # `makedirs` of a shared parent.
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                except FileExistsError:
                    pass

                shutil.copy2(source, target, follow_symlinks=False)

            except OSError as error:
                self.debug('skipping {}, it could not be staged for a batched transfer: {}'.format(source, error))
                continue

            staged[relpath] = (source, entry)

        return tree_root, staged, fallback

    def write_batch_list(self, batch_root: str, relpaths: List[str]) -> str:
        """
        Write the ``--files-from`` list into ``batch_root``, next to and never inside the transfer
        root, so that neither the transfer itself nor ``hide_secrets`` can touch it.

        The entries are NUL separated, to be read back with ``--from0``, which keeps the list correct
        even for a path containing a newline. Written as bytes on purpose: text mode would translate
        such a newline and corrupt the entry, and ``os.fsencode`` round trips whatever encoding the
        paths came from.
        """

        list_file = os.path.join(batch_root, BATCH_LIST_FILE_NAME)

        with open(list_file, 'wb') as f:
            for relpath in relpaths:
                f.write(os.fsencode(relpath) + b'\0')

        return list_file

    def batch_destination(self, archive_mode: str) -> str:
        """
        Destination root of a batched transfer, the request directory. The per file paths under it
        come from the ``--files-from`` list.
        """

        assert self._request_id is not None

        if archive_mode == 'daemon':
            return 'rsync://{}/{}/'.format(self.artifacts_rsync_host, self._request_id)

        if archive_mode == 'ssh':
            return '{}:{}/'.format(self.artifacts_host, os.path.join(self.artifacts_root, self._request_id))

        return os.path.join(self.artifacts_local_root, self._request_id) + os.sep

    def run_rsync_batch(self, archive_mode: str, list_file: str, tree_root: str, relpaths: List[str]) -> bool:
        """
        Transfer all ``relpaths`` under ``tree_root`` with a single rsync invocation.

        :returns: ``True`` if the transfer finished, ``False`` if it exhausted its retries and the
            caller has to transfer the files one by one instead.
        """

        destination = self.batch_destination(archive_mode)

        cmd = ['rsync'] + self.rsync_transfer_options + [
            '--files-from={}'.format(list_file),
            '--from0',
            # `--files-from` implies `--relative`, but an explicit `--no-relative` in `rsync-options`
            # would override that, flatten the paths and let files sharing a name overwrite each other.
            '--relative',
            # The staged files cannot vanish under rsync, but a future non-copy batch could.
            '--ignore-missing-args',
            # Let rsync create the missing parent directories with default attributes instead of
            # copying them from the freshly created staging tree. Without this `--times` would
            # rewrite the mtime of every remote directory on every progress tick, and we would
            # still need the `ssh mkdir -p` round trips.
            '--no-implied-dirs',
            tree_root + os.sep,
            destination,
        ]

        def _run_rsync() -> Result[bool, bool]:
            try:
                Command(cmd, logger=self.logger).run()

            except gluetool.GlueCommandError as exc:
                if exc.output.exit_code in RSYNC_PARTIAL_EXIT_CODES:
                    self.warn(
                        'batched rsync of {} files finished with exit code {}, some files were not '
                        'transferred: {}'.format(len(relpaths), exc.output.exit_code, exc),
                        # Vanishing sources are expected for logs which are still being written to,
                        # a partial transfer due to an error is not.
                        sentry=exc.output.exit_code != RSYNC_VANISHED_EXIT_CODE
                    )
                    return Result.Ok(True)

                self.warn('rsync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)

            return Result.Ok(True)

        try:
            gluetool.utils.wait(
                "batched rsync of {} files to '{}'".format(len(relpaths), destination),
                _run_rsync,
                timeout=self.option('retry-timeout'),
                tick=self.option('retry-tick')
            )

        except GlueError as error:
            self.warn(
                'Batched rsync using {} failed, falling back to per-file archiving: {}'.format(archive_mode, error),
                sentry=True
            )
            return False

        return True

    def archive_staged_source(
        self,
        tree_root: str,
        relpath: str,
        source: str,
        entry: Dict[str, Any],
        archive_mode: str
    ) -> None:
        """
        Archive a single staged and already redacted file of a batch, with the very arguments the per
        file path would use for its own copy of ``source``.
        """

        staged_source = os.path.join(tree_root, relpath)
        destination = entry.get('destination', '')

        try:
            if archive_mode == 's3':
                self.run_aws(staged_source, destination, original_source=source)

            else:
                self.run_rsync(staged_source, destination, archive_mode, original_source=source)

        except Exception as error:
            # A failed destination must not prevent archiving to the remaining ones.
            self.error(f'Failed to sync {source} using {archive_mode}: {error}', sentry=True)

    def run_batch(
        self,
        stage: str,
        chunk: List[Tuple[str, Dict[str, Any]]]
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Stage and redact the ``chunk`` sources once, then archive them with every selected mode: one
        rsync invocation per rsync mode, and one upload per file for ``s3``.

        :param chunk: pairs of a source and its map entry.
        :returns: sources the caller has to archive one by one on the per file path instead. All of
            them when preparing the batch fails, otherwise those :py:meth:`prepare_batch_tree` could
            not stage safely.
        """

        batch_root = tempfile.mkdtemp()

        try:
            # An exception in here leaves before any transfer: no mode ever sees a batch whose staging,
            # filtering or redaction failed, and the per file path makes a fresh copy of each source
            # and runs its own `hide_secrets`.
            try:
                tree_root, staged, fallback = self.prepare_batch_tree(batch_root, chunk)

                if not staged:
                    self.debug('no files staged for a batched transfer')
                    return fallback

                relpaths = sorted(staged)

                log_dict(self.debug, 'batched files', relpaths)

                rsync_relpaths, single_relpaths = self.rsync_filter_relpaths(batch_root, relpaths)

                list_file = self.write_batch_list(batch_root, rsync_relpaths)

                # A single pass over the staged tree, instead of one `find | xargs sed` and one `sync`
                # per file.
                self.shared('hide_secrets', search_path=tree_root)

            except Exception as error:
                self.warn(
                    'Preparing batched archiving failed, falling back to per-file archiving: {}'.format(error),
                    sentry=True
                )
                return chunk

            for archive_mode in self.archive_modes:
                if self._archiving_cancelled(stage):
                    self.debug('Archiving cancelled, stopping batched archiving')
                    return []

                # S3 does not apply the rsync filter rules, it gets every staged file.
                if archive_mode == 's3':
                    one_by_one = relpaths

                elif not rsync_relpaths or self.run_rsync_batch(archive_mode, list_file, tree_root, rsync_relpaths):
                    one_by_one = single_relpaths

                # The staged files are redacted already, a failed batch falls back to them rather than
                # to fresh copies.
                else:
                    one_by_one = rsync_relpaths + single_relpaths

                for relpath in one_by_one:
                    if self._archiving_cancelled(stage):
                        self.debug('Archiving cancelled, stopping batched archiving')
                        return []

                    source, entry = staged[relpath]
                    self.archive_staged_source(tree_root, relpath, source, entry, archive_mode)

        finally:
            shutil.rmtree(batch_root, ignore_errors=True)

        return fallback

    def batch_chunks(self, batch: List[Tuple[str, Dict[str, Any]]]) -> List[List[Tuple[str, Dict[str, Any]]]]:
        """
        Split ``batch`` into chunks of at most ``batch-max-files`` files and ``batch-max-size`` MiB, to
        bound how much a single failed batch can cost and how much temporary space its staged copies
        take. Zero means no limit. A file larger than the size limit forms a chunk of its own, which is
        then archived on the per file path.
        """

        max_files = self.option('batch-max-files') or 0
        max_bytes = (self.option('batch-max-size') or 0) * 1024 * 1024

        chunks: List[List[Tuple[str, Dict[str, Any]]]] = []
        chunk: List[Tuple[str, Dict[str, Any]]] = []
        chunk_bytes = 0

        for item in batch:
            try:
                size = os.lstat(item[0]).st_size

            # Staging drops a file which vanished, it takes no space.
            except OSError:
                size = 0

            if chunk and (
                (max_files > 0 and len(chunk) >= max_files)
                or (max_bytes > 0 and chunk_bytes + size > max_bytes)
            ):
                chunks.append(chunk)
                chunk, chunk_bytes = [], 0

            chunk.append(item)
            chunk_bytes += size

        if chunk:
            chunks.append(chunk)

        return chunks

    def run_rsync(
        self,
        source: str,
        destination: str,
        archive_mode: str,
        options: Optional[List[str]] = None,
        original_source: Optional[str] = None
    ) -> None:
        options = options or []

        cmd = ['rsync']

        cmd += self.rsync_options

        if options:
            cmd += options

        cmd.append(source)

        # Reuse source directory name as destination if destination is not set
        # This is useful when we want to sync particular files
        # from the source directory without losing the directory structure
        if not destination:
            destination = os.path.dirname(original_source or source)
            destination = destination.lstrip('/')

        if os.path.isdir(destination) and destination not in ['/', '.']:
            if archive_mode == 'daemon':
                self.create_archive_directory_rsync(destination)
            elif archive_mode == 'ssh':
                self.create_archive_directory_ssh(destination)
            else:
                self.create_archive_directory_local(destination)

        # In case we are working with a copy, the destination must be set to the original source,
        # because the source and destination are different
        if original_source is not None:
            destination = original_source
            destination = destination.lstrip('/')

        assert self._request_id is not None

        if archive_mode == 'daemon':
            full_destination = 'rsync://{}/{}'.format(
                self.artifacts_rsync_host,
                os.path.join(self._request_id, destination)
            )

        elif archive_mode == 'ssh':
            full_destination = '{}:{}'.format(
                self.artifacts_host,
                os.path.join(self.artifacts_root, self._request_id, destination)
            )

        else:
            full_destination = os.path.join(self.artifacts_local_root, self._request_id, destination)

        cmd.append(full_destination)

        # Check if source file or directory still exists
        if not os.path.exists(source):
            self.warn('source {} does not exist, skipping rsync'.format(source))
            return

        self.debug('syncing {} to {}'.format(source, full_destination))

        def _run_rsync() -> Result[bool, bool]:
            try:
                Command(cmd, logger=self.logger).run()
            except gluetool.GlueCommandError as exc:
                # The source disappeared while rsync was reading it, which is expected for logs still
                # being written to. Retrying cannot bring it back, and each retry costs another
                # connection to the artifacts host.
                if exc.output.exit_code == RSYNC_VANISHED_EXIT_CODE:
                    self.warn('source {} vanished during rsync, skipping'.format(source))
                    return Result.Ok(True)

                self.warn('rsync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)
            return Result.Ok(True)

        gluetool.utils.wait(
            "rsync '{}' to '{}'".format(source, destination),
            _run_rsync,
            timeout=self.option('retry-timeout'),
            tick=self.option('retry-tick')
        )

    def run_aws(
        self,
        source: str,
        destination: str,
        options: Optional[List[str]] = None,
        original_source: Optional[str] = None
    ) -> None:
        options = options or []

        env = os.environ.copy()
        env.update({
            'AWS_REGION': self.option('aws-region'),
            'AWS_ACCESS_KEY_ID': self.option('aws-access-key-id'),
            'AWS_SECRET_ACCESS_KEY': self.option('aws-secret-access-key'),
        })

        cmd = ['aws', 's3']

        if os.path.isdir(source):
            cmd.append('sync')
        else:
            cmd.append('cp')

        cmd += self.aws_options

        if options:
            cmd += options

        cmd.append(source)

        # Reuse source directory name as destination if destination is not set
        # This is useful when we want to sync particular files
        # from the source directory without losing the directory structure

        # In case we are working with a copy, the destination must be set to the original source,
        # because the source and destination are different
        if not destination or original_source is not None:
            destination = original_source or source
            destination = destination.lstrip('/').lstrip('./')

        assert self._request_id is not None

        full_destination = 's3://{}{}'.format(
            self.option('aws-s3-bucket'),
            os.path.join(self.artifacts_root, self._request_id, destination)
        )

        cmd.append(full_destination)

        # Check if source file or directory still exists
        if not os.path.exists(source):
            self.warn('source {} does not exist, skipping aws'.format(source))
            return

        self.debug('syncing {} to {}'.format(source, full_destination))

        def _run_aws() -> Result[bool, bool]:
            try:
                Command(cmd, logger=self.logger).run(env=env)
            except gluetool.GlueCommandError as exc:
                self.warn('aws command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)
            return Result.Ok(True)

        gluetool.utils.wait(
            "s3 sync '{}' to '{}'".format(source, destination),
            _run_aws,
            timeout=self.option('retry-timeout'),
            tick=self.option('retry-tick')
        )

    # The stage is default to progress because we want to use the function
    # in the parallel archiving timer without calling it
    def archive_stage(self, stage: str = 'progress') -> None:

        if not self._request_id:
            self.warn('No testing farm request found, skipping archiving', sentry=True)
            return

        # Before archiving in progress, let's regenerate results.xml
        if stage == 'progress':
            # Refresh results from results.yaml for running schedule entries
            # This allows partial test results to be shown during progress
            schedule = self.shared('test_schedule')
            if schedule and self.get_shared('refresh_test_schedule_entry_results'):
                for entry in schedule:
                    self.shared('refresh_test_schedule_entry_results', entry)

            self.shared('generate_results', 'test execution running', generate_xunit=False, report_results=False)

        map_stage = self.source_destination_map().get(stage, [])

        # Glob matches deferred to batched transfers, flushed once the whole map has been walked. Pairs
        # of a source and its map entry.
        batch: List[Tuple[str, Dict[str, Any]]] = []

        for entry in map_stage:
            if entry.get('source') is None:
                raise GlueError('Source path must be specified in source-destination-map')

            sources = entry['source']
            excludes = entry.get('exclude')
            verify = normalize_bool_option(entry.get('verify'))

            if verify:
                self.require_shared('artifacts_location')

            # If the entry['source'] is a wildcard, we need to use glob to find all the files
            for source in glob(sources, recursive=True):

                if self._archiving_cancelled(stage):
                    self.debug('Archiving cancelled, stopping progress sync')
                    return

                try:
                    if excludes is not None and any(re.search(exclude_entry, source) for exclude_entry in excludes):
                        self.debug('Skipping {} because it matches exclude pattern {}'.format(source, excludes))
                        continue

                    if self.is_batchable(stage, entry, source):
                        batch.append((source, entry))
                        continue

                except Exception as error:
                    # Log error and continue with another item
                    self.error(f'Failed to sync {sources}: {error}', sentry=True)
                    continue

                self.archive_source(stage, entry, source)

        for chunk in self.batch_chunks(batch):
            if self._archiving_cancelled(stage):
                self.debug('Archiving cancelled, dropping the remaining batched files')
                return

            # A batch of one is never a win, it only adds a list file to an otherwise identical transfer.
            fallback = self.run_batch(stage, chunk) if len(chunk) > 1 else chunk

            # Whatever the batch could not archive goes through the very same code path a non-batched
            # pipeline uses.
            for source, entry in fallback:
                if self._archiving_cancelled(stage):
                    self.debug('Archiving cancelled, stopping progress sync')
                    return

                self.archive_source(stage, entry, source)

    def archive_source(self, stage: str, entry: Dict[str, Any], source: str) -> None:
        """
        Archive a single glob match of a map ``entry`` with every selected archive mode.
        """

        destination = entry.get('destination', '')
        permissions = entry.get('permissions')
        verify = normalize_bool_option(entry.get('verify') or False)

        source_copy: Optional[str] = None
        try:
            archive_source = source
            original_source: Optional[str] = None
            # hide-secrets uses 'sed -i', which changes the inode and breaks writing to live logs.
            # Prepare one redacted snapshot for all modes in the 'progress' and 'execute' stages.
            if stage in ARCHIVE_STAGES_USING_COPY:
                source_copy, original_source = self.prepare_source_copy(source)
                archive_source = source_copy

            self.shared('hide_secrets', search_path=archive_source)
            verify_archive = any(mode != 'local' for mode in self.archive_modes)

            for archive_mode in self.archive_modes:
                try:
                    options = []

                    if archive_mode != 's3':
                        # Not needed for S3
                        if os.path.isdir(source):
                            options.append('--recursive')

                        # S3 does not support permissions
                        if permissions:
                            options.append('--chmod={}'.format(permissions))

                    if archive_mode == 's3':
                        self.run_aws(
                            archive_source, destination,
                            options=options or None,
                            original_source=original_source
                        )
                    else:
                        self.run_rsync(
                            archive_source, destination, archive_mode,
                            options=options or None,
                            original_source=original_source
                        )

                except Exception as error:
                    # A failed destination must not prevent archiving to the remaining ones.
                    verify_archive = False
                    self.error(f'Failed to sync {source} using {archive_mode}: {error}', sentry=True)

            if not verify or not verify_archive:
                return

            # Verify only the coldstore location, not all archive destinations.
            # Skip verification after any upload failure: the failed mode may serve this URL.
            target = self.shared('artifacts_location', source.lstrip('/'))

            def _verify_archivation() -> Result[bool, bool]:
                with gluetool.utils.requests() as request:
                    # For HEAD method we need to enable redirects explicitely
                    # https://requests.readthedocs.io/en/latest/user/quickstart/#redirection-and-history
                    response = request.head(target, allow_redirects=True)

                    if response.status_code == 200:
                        return Result.Ok(True)

                    return Result.Error(True)

            self.info("Verifying archivation of '{}'".format(target))

            gluetool.utils.wait(
                "verify archivation of '{}'".format(target),
                _verify_archivation,
                timeout=self.option('verify-timeout'),
                tick=self.option('verify-tick')
            )

        except Exception as error:
            # Log error and continue with another item
            self.error(f'Failed to sync {entry["source"]}: {error}', sentry=True)
        finally:
            if source_copy is not None:
                self.delete_source_copy(source_copy)

    def _safe_archive_stage(self, stage: str = 'progress') -> None:
        """
        A wrapper around archive_stage that catches all exceptions to prevent thread from getting stuck.
        """

        self.debug('Parallel archiving started')
        try:
            self.archive_stage(stage)
        except Exception as exc:
            # Log the exception but don't let it propagate and crash the thread
            self.error(f"Error during parallel archiving: {exc}", sentry=True)
        self.debug('Parallel archiving finished')

    def execute(self) -> None:
        if self.option('disable-archiving'):
            self.info('Archiving is disabled, skipping')
            return

        if not self.get_shared('testing_farm_request') or not self.shared('testing_farm_request'):
            self.warn('No testing farm request found, skipping archiving', sentry=True)
            return

        self._request_id = self.shared('testing_farm_request').id

        # Only needed for mypy, it can't be None at this point
        assert self._request_id is not None

        for archive_mode in self.archive_modes:
            if archive_mode == 'ssh':
                self.create_archive_directory_ssh()
            elif archive_mode == 'daemon':
                self.create_archive_directory_rsync()
            elif archive_mode == 'local':
                self.create_archive_directory_local()
            # S3 does not need to create directories

        self.archive_stage('execute')

        if self.option('enable-parallel-archiving'):
            self.info('Starting parallel archiving')

            parallel_archiving_tick = self.option('parallel-archiving-tick')
            self._archive_timer = RepeatTimer(
                parallel_archiving_tick,
                self._safe_archive_stage
            )

            self.debug('Starting parallel archiving, run every {} seconds'.format(parallel_archiving_tick))

            self._archive_timer.start()

    def destroy(self, failure: Optional[gluetool.Failure] = None) -> None:
        if self.option('disable-archiving'):
            self.info('Archiving is disabled, skipping')
            return

        if self.option('enable-parallel-archiving'):
            self.info('Stopping parallel archiving')
            if self._archive_timer:
                self._archive_timer.cancel()

                # Wait until the timer is finished
                self.debug('Waiting for parallel archiving to finish')

                def _wait_for_timer() -> Result[bool, bool]:
                    if self._archive_timer and self._archive_timer.is_alive():
                        return Result.Error(True)
                    return Result.Ok(True)

                try:
                    gluetool.utils.wait(
                        "wait for parallel archiving to finish",
                        _wait_for_timer,
                        timeout=self.option('parallel-archiving-finish-timeout'),
                        tick=self.option('parallel-archiving-finish-tick')
                    )
                except GlueError:
                    self.error('Parallel archiving did not finish in time', sentry=True)

                self._archive_timer = None

        # Gracefully catch errors, so other destroy functions can get a chance.
        # Send the error to Sentry so we know this is happening.
        self.shared('trigger_event', PIPELINE_ARCHIVING_STARTED)
        try:
            self.archive_stage('destroy')
        except GlueError as error:
            self.error(str(error), sentry=True)
        except Exception as error:
            self.error('Unexpected error during destroy stage archiving: {}'.format(error), sentry=True)
        self.shared('trigger_event', PIPELINE_ARCHIVING_FINISHED)
