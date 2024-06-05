# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
from glob import glob

import gluetool
from gluetool.glue import GlueError
from gluetool.utils import Command, normalize_bool_option, render_template
from gluetool.result import Result
from gluetool_modules_framework.libs.threading import RepeatTimer

from typing import List, Optional, Any

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10
DEFAULT_PARALLEL_ARCHIVING_TICK = 30
DEFAULT_RSYNC_TIMEOUT = 120
DEFAULT_VERIFY_TICK = 5
DEFAULT_VERIFY_TIMEOUT = 600

ARCHIVE_STAGES = ['execute', 'progress', 'destroy']
# Stages which use a copy for syncing
ARCHIVE_STAGES_USING_COPY = ['execute', 'progress']
SOURCE_DESTINATION_ENTRY_KEYS = ['source', 'destination', 'permissions', 'verify']


class Archive(gluetool.Module):
    """
    The module is used to archive artifacts from the testing farm to a remote location.

    The module supports SOURCE_DESTINATION_MAP environment variable which can be used to specify
    additional source-destination mappings. The variable is a string of the following format:
    <SOURCE>:[<DESTINATION>]:[<PERMISSIONS>]:[<STAGE>][#<SOURCE>:[<DESTINATION>]:[<PERMISSIONS>][<STAGE>]]...

    The ``rsync-mode`` option supports three modes: ``daemon``, ``ssh`` and ``local``:
    * The ``daemon`` mode uses the rsync daemon to with rsync protocol
    * The ``ssh`` mode uses rsync with ssh protocol
    * The ``local`` mode copies files locally with rsync. It should be used only for testing and development.

    Use the ``verify`` flag to verify given path on the artifact location provided by the ``coldstore`` module.
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
        'rsync-mode': {
            'help': 'Rsync mode to use.',
            'choices': ['daemon', 'ssh', 'local'],
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
        }
    }

    required_options = ('source-destination-map', 'artifacts-root', 'rsync-mode',)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(Archive, self).__init__(*args, **kwargs)

        self._archive_timer: Optional[RepeatTimer] = None
        # List of created directories on the host.
        # We need to keep track of them to avoid creating them multiple times.
        self._created_directories: List[str] = []

    def sanity(self) -> None:
        if self.option('rsync-mode') not in ('daemon', 'ssh', 'local'):
            raise GlueError('rsync mode must be either daemon, ssh or local')

        if self.option('rsync-mode') == 'daemon' and not self.option('artifacts-rsync-host'):
            raise GlueError('rsync daemon host must be specified when using rsync daemon mode')

        if self.option('rsync-mode') == 'ssh' and not self.option('artifacts-host'):
            raise GlueError('artifacts host must be specified when using ssh mode')

        if self.option('rsync-mode') == 'local' and not self.option('artifacts-local-root'):
            raise GlueError('artifacts local root must be specified when using local mode')

    def source_destination_map(self) -> Any:
        source_destination_map = gluetool.utils.load_yaml(
            gluetool.utils.normalize_path(self.option('source-destination-map')),
        )

        additional_map_var = os.environ.get('SOURCE_DESTINATION_MAP', None)
        if additional_map_var:
            for entry in additional_map_var.split('#'):

                source, destination, permissions, stage = entry.split(':')

                if stage not in ARCHIVE_STAGES:
                    raise GlueError('Invalid stage "{}" in SOURCE_DESTINATION_MAP env'.format(stage))

                source_destination_map[stage].append({
                    'source': source,
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

                    if value is not None:
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

    def create_archive_directory_ssh(self, directory: Optional[str] = None) -> None:
        """
        Creates directory on the host with ssh where artifacts will be stored.
        If directory is not set, it will create the root directory.
        """
        request_id = self.shared('testing_farm_request').id

        path = os.path.join(self.artifacts_root, request_id)
        if directory:
            path = os.path.join(path, directory)

        if path in self._created_directories:
            return

        cmd: List[str] = [
            'ssh',
            self.artifacts_host,
            'mkdir',
            '-p',
            path
        ]

        Command(cmd, logger=self.logger).run()
        self._created_directories.append(path)

    def create_archive_directory_rsync(self, directory: Optional[str] = None) -> None:
        """
        Creates directory on the host with rsync where artifacts will be stored.
        If directory is not set, it will create the root directory.
        """
        path = self.shared('testing_farm_request').id

        if directory:
            path = os.path.join(path, directory)

        if path in self._created_directories:
            return

        cmd = ['rsync']

        cmd += self.rsync_options

        cmd.append('/dev/null')

        cmd.append('rsync://{}/{}/'.format(
                self.artifacts_rsync_host,
                path
        ))

        Command(cmd, logger=self.logger).run()

        self._created_directories.append(path)

    def create_archive_directory_local(self, directory: Optional[str] = None) -> None:
        """
        Creates directory on the local host.
        """
        request_id = self.shared('testing_farm_request').id

        path = os.path.join(self.artifacts_local_root, request_id)

        if directory:
            path = os.path.join(path, directory)

        if path in self._created_directories:
            return

        cmd = [
            'mkdir',
            '-p',
            path
        ]

        Command(cmd, logger=self.logger).run()

        self._created_directories.append(path)

    def run_rsync(
        self,
        source: str,
        destination: str,
        options: Optional[List[str]] = None,
        source_copy: bool = False
    ) -> None:
        options = options or []
        original_source = source

        request_id = self.shared('testing_farm_request').id

        cmd = ['rsync']

        cmd += self.rsync_options

        if options:
            cmd += options

        # Used in cases when we need to work with a source copy to mitigate breaking of "live" logs
        if source_copy:
            # get rid of a possible slash at the end, it would cause issues
            original_source = original_source.rstrip('/')
            source = '{}.copy'.format(original_source)
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

        cmd.append(source)

        # Reuse source directory name as destination if destination is not set
        # This is useful when we want to sync particular files
        # from the source directory without losing the directory structure
        if not destination:
            destination = os.path.dirname(original_source)
            destination = destination.lstrip('/')

        if os.path.isdir(destination) and destination not in ['/', '.']:
            if self.option('rsync-mode') == 'daemon':
                self.create_archive_directory_rsync(destination)
            elif self.option('rsync-mode') == 'ssh':
                self.create_archive_directory_ssh(destination)
            else:
                self.create_archive_directory_local(destination)

        # In case we are working with a copy, the destination must be set to the original source,
        # because the source and destination are different
        if source_copy:
            destination = original_source
            destination = destination.lstrip('/')

        if self.option('rsync-mode') == 'daemon':
            full_destination = 'rsync://{}/{}'.format(
                self.artifacts_rsync_host,
                os.path.join(request_id, destination)
            )

        elif self.option('rsync-mode') == 'ssh':
            full_destination = '{}:{}'.format(
                self.artifacts_host,
                os.path.join(self.artifacts_root, request_id, destination)
            )

        else:
            full_destination = os.path.join(self.artifacts_local_root, request_id, destination)

        # Before we start archiving, we need to hide secrets in files
        self.shared('hide_secrets', search_path=source)

        cmd.append(full_destination)
        self.debug('syncing {} to {}'.format(source, full_destination))

        def _run_rsync() -> Result[bool, bool]:
            try:
                Command(cmd, logger=self.logger).run()
            except gluetool.GlueCommandError as exc:
                self.warn('rsync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)
            return Result.Ok(True)

        gluetool.utils.wait(
            "rsync '{}' to '{}'".format(source, destination),
            _run_rsync,
            timeout=self.option('retry-timeout'),
            tick=self.option('retry-tick')
        )

        # make sure to remove source copy if it was created
        if source_copy:
            self.debug('removing source copy {}'.format(source))
            if os.path.isdir(source):
                shutil.rmtree(source)
            else:
                os.unlink(source)

    # The stage is default to progress because we want to use the function
    # in the parallel archiving timer without calling it
    def archive_stage(self, stage: str = 'progress') -> None:

        map_stage = self.source_destination_map().get(stage, [])

        for entry in map_stage:
            if entry.get('source') is None:
                raise GlueError('Source path must be specified in source-destination-map')

            sources = entry['source']
            destination = entry.get('destination')
            permissions = entry.get('permissions')
            verify = normalize_bool_option(entry.get('verify'))

            if verify:
                self.require_shared('artifacts_location')

            # If the entry['source'] is a wildcard, we need to use glob to find all the files
            for source in glob(sources, recursive=True):

                options = []

                if os.path.isdir(source):
                    options.append('--recursive')

                if permissions:
                    options.append('--chmod={}'.format(permissions))

                # In case of syncing 'progress' and 'execute' stages, make sure we work with a copy.
                # This is a workaround for the problem of the source file being overwritten with hide-secrets module.
                # In that module 'sed -i' is used and the inode of the source file would change, effectively breaking
                # the saving of the "live" logs like 'progress.log'.
                self.run_rsync(
                    source, destination,
                    options=options or None,
                    source_copy=True if stage in ARCHIVE_STAGES_USING_COPY else False
                )

                if not verify or self.option('rsync-mode') == 'local':
                    continue

                # Verify archivation target
                target = self.shared('artifacts_location', source.lstrip('/'))

                def _verify_archivation() -> Result[bool, bool]:
                    with gluetool.utils.requests() as request:
                        response = request.head(target)

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

    def execute(self) -> None:
        if self.option('disable-archiving'):
            self.info('Archiving is disabled, skipping')
            return

        if self.option('rsync-mode') == 'ssh':
            self.create_archive_directory_ssh()
        elif self.option('rsync-mode') == 'daemon':
            self.create_archive_directory_rsync()
        else:
            self.create_archive_directory_local()

        self.archive_stage('execute')

        if self.option('enable-parallel-archiving'):
            self.info('Starting parallel archiving')

            parallel_archiving_tick = self.option('parallel-archiving-tick')
            self._archive_timer = RepeatTimer(
                parallel_archiving_tick,
                self.archive_stage
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
                self._archive_timer = None

        # Gracefully catch errors, so other destroy functions can get a chance.
        # Send the error to Sentry so we know this is happening.
        try:
            self.archive_stage('destroy')
        except GlueError as error:
            self.error(str(error), sentry=True)
