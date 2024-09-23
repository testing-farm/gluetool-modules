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

from typing import List, Optional, Any, Tuple

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10
DEFAULT_PARALLEL_ARCHIVING_TICK = 30
DEFAULT_RSYNC_TIMEOUT = 120
DEFAULT_VERIFY_TICK = 5
DEFAULT_VERIFY_TIMEOUT = 600
DEFAULT_PARALLEL_ARCHIVING_FINISH_TICK = 5
DEFAULT_PARALLEL_ARCHIVING_FINISH_TIMEOUT = 600

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

    The ``archive-mode`` option supports four modes: ``daemon``, ``ssh``, ``local`` and ``s3``:
    * The ``daemon`` mode uses the rsync daemon to with rsync protocol
    * The ``ssh`` mode uses rsync with ssh protocol
    * The ``local`` mode copies files locally with rsync. It should be used only for testing and development.
    * The ``s3`` mode uses the AWS cli to sync files to S3 bucket.

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
        'archive-mode': {
            'help': 'Archive mode to use.',
            'choices': ['daemon', 'ssh', 'local', 's3'],
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
        # List of created directories on the host.
        # We need to keep track of them to avoid creating them multiple times.
        self._created_directories: List[str] = []

    def sanity(self) -> None:
        if self.option('archive-mode') not in ('daemon', 'ssh', 'local', 's3'):
            raise GlueError('rsync mode must be either daemon, ssh, local or s3')

        if self.option('archive-mode') == 'daemon' and not self.option('artifacts-rsync-host'):
            raise GlueError('rsync daemon host must be specified when using rsync daemon mode')

        if self.option('archive-mode') == 'ssh' and not self.option('artifacts-host'):
            raise GlueError('artifacts host must be specified when using ssh mode')

        if self.option('archive-mode') == 'local' and not self.option('artifacts-local-root'):
            raise GlueError('artifacts local root must be specified when using local mode')

        if self.option('archive-mode') == 's3':
            for option in ['aws-region', 'aws-s3-bucket', 'aws-access-key-id', 'aws-secret-access-key']:
                if not self.option(option):
                    raise GlueError('{} option must be specified when using s3 mode')

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

    def prepare_source_copy(self, original_source: str) -> Tuple[str, str]:
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

        return source, original_source

    def delete_source_copy(self, source: str) -> None:
        self.debug('removing source copy {}'.format(source))
        if os.path.isdir(source):
            shutil.rmtree(source)
        else:
            os.unlink(source)

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
            source, original_source = self.prepare_source_copy(original_source)

        cmd.append(source)

        # Reuse source directory name as destination if destination is not set
        # This is useful when we want to sync particular files
        # from the source directory without losing the directory structure
        if not destination:
            destination = os.path.dirname(original_source)
            destination = destination.lstrip('/')

        if os.path.isdir(destination) and destination not in ['/', '.']:
            if self.option('archive-mode') == 'daemon':
                self.create_archive_directory_rsync(destination)
            elif self.option('archive-mode') == 'ssh':
                self.create_archive_directory_ssh(destination)
            else:
                self.create_archive_directory_local(destination)

        # In case we are working with a copy, the destination must be set to the original source,
        # because the source and destination are different
        if source_copy:
            destination = original_source
            destination = destination.lstrip('/')

        if self.option('archive-mode') == 'daemon':
            full_destination = 'rsync://{}/{}'.format(
                self.artifacts_rsync_host,
                os.path.join(request_id, destination)
            )

        elif self.option('archive-mode') == 'ssh':
            full_destination = '{}:{}'.format(
                self.artifacts_host,
                os.path.join(self.artifacts_root, request_id, destination)
            )

        else:
            full_destination = os.path.join(self.artifacts_local_root, request_id, destination)

        # Before we start archiving, we need to hide secrets in files
        self.shared('hide_secrets', search_path=source)

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
            self.delete_source_copy(source)

    def run_aws(
        self,
        source: str,
        destination: str,
        options: Optional[List[str]] = None,
        source_copy: bool = False
    ) -> None:
        options = options or []
        original_source = source

        request_id = self.shared('testing_farm_request').id

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

        # Used in cases when we need to work with a source copy to mitigate breaking of "live" logs
        if source_copy:
            source, original_source = self.prepare_source_copy(original_source)

        cmd.append(source)

        # Reuse source directory name as destination if destination is not set
        # This is useful when we want to sync particular files
        # from the source directory without losing the directory structure

        # In case we are working with a copy, the destination must be set to the original source,
        # because the source and destination are different
        if not destination or source_copy:
            destination = original_source
            destination = destination.lstrip('/').lstrip('./')

        full_destination = 's3://{}{}'.format(
            self.option('aws-s3-bucket'),
            os.path.join(self.artifacts_root, request_id, destination)
        )

        # Before we start archiving, we need to hide secrets in files
        self.shared('hide_secrets', search_path=source)

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
                self.warn('rsync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc))
                return Result.Error(False)
            return Result.Ok(True)

        gluetool.utils.wait(
            "s3 sync '{}' to '{}'".format(source, destination),
            _run_aws,
            timeout=self.option('retry-timeout'),
            tick=self.option('retry-tick')
        )

        # make sure to remove source copy if it was created
        if source_copy:
            self.delete_source_copy(source)

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

                # .copy files should be ignored here, they are created and should be deleted in run_* functions
                if source.endswith('.copy'):
                    continue

                options = []

                if self.option('archive-mode') != 's3':
                    # Not needed for S3
                    if os.path.isdir(source):
                        options.append('--recursive')

                    # S3 does not support permissions
                    if permissions:
                        options.append('--chmod={}'.format(permissions))

                # In case of syncing 'progress' and 'execute' stages, make sure we work with a copy.
                # This is a workaround for the problem of the source file being overwritten with hide-secrets module.
                # In that module 'sed -i' is used and the inode of the source file would change, effectively breaking
                # the saving of the "live" logs like 'progress.log'.
                if self.option('archive-mode') == 's3':
                    self.run_aws(
                        source, destination,
                        options=options or None,
                        source_copy=True if stage in ARCHIVE_STAGES_USING_COPY else False
                    )
                else:
                    self.run_rsync(
                        source, destination,
                        options=options or None,
                        source_copy=True if stage in ARCHIVE_STAGES_USING_COPY else False
                    )

                if not verify or self.option('archive-mode') == 'local':
                    continue

                # Verify archivation target
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

    def execute(self) -> None:
        if self.option('disable-archiving'):
            self.info('Archiving is disabled, skipping')
            return

        if self.option('archive-mode') == 'ssh':
            self.create_archive_directory_ssh()
        elif self.option('archive-mode') == 'daemon':
            self.create_archive_directory_rsync()
        elif self.option('archive-mode') == 'local':
            self.create_archive_directory_local()
        # S3 does not need to create directories

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

                # Wait until the timer is finished
                self.debug('Waiting for parallel archiving to finish')

                def _wait_for_timer() -> Result[bool, bool]:
                    if self._archive_timer and self._archive_timer.is_alive():
                        return Result.Error(True)
                    return Result.Ok(True)

                gluetool.utils.wait(
                    "wait for parallel archiving to finish",
                    _wait_for_timer,
                    timeout=self.option('parallel-archiving-finish-timeout'),
                    tick=self.option('parallel-archiving-finish-tick')
                )

                self.debug('Parallel archiving finished')

                self._archive_timer = None

        # Gracefully catch errors, so other destroy functions can get a chance.
        # Send the error to Sentry so we know this is happening.
        try:
            self.archive_stage('destroy')
        except GlueError as error:
            self.error(str(error), sentry=True)
