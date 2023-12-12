# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
from glob import glob

import gluetool
from gluetool.glue import GlueError
from gluetool.utils import Command, render_template
from gluetool.result import Result
from gluetool_modules_framework.libs.threading import RepeatTimer

from typing import List, Optional, Any

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10
DEFAULT_PARALLEL_ARCHIVING_TICK = 30


class Archive(gluetool.Module):
    """
    The module is used to archive artifacts from the testing farm to a remote location.

    The module supports SOURCE_DESTINATION_MAP environment variable which can be used to specify
    additional source-destination mappings. The variable is a string of the following format:
    <SOURCE>:[<DESTINATION>]:[<PERMISSIONS>][#<SOURCE>:[<DESTINATION>]:[<PERMISSIONS>]]...
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
        'source-destination-map': {
            'help': 'Mapping of source to destination paths and permissions.',
            'metavar': 'FILE'
        },
        'rsync-mode': {
            'help': 'Rsync mode to use.',
            'choices': ['daemon', 'ssh'],
        },
        'rsync-options': {
            'help': 'Rsync options to use.',
            'action': 'append',
            'default': []
        },
        'retry-tick': {
            'help': 'Number of retries for failed rsync operations. (default: %(default)s)',
            'metavar': 'RETRY_TICK',
            'type': int,
            'default': DEFAULT_RETRY_TICK,
        },
        'retry-timeout': {
            'help': 'Timeout between rsync retries in seconds. (default: %(default)s)',
            'metavar': 'RETRY_TIMEOUT',
            'type': int,
            'default': DEFAULT_RETRY_TIMEOUT,
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

    def sanity(self) -> None:
        if self.option('rsync-mode') not in ('daemon', 'ssh'):
            raise GlueError('rsync mode must be either daemon or ssh')

        if self.option('rsync-mode') == 'daemon' and not self.option('artifacts-rsync-host'):
            raise GlueError('rsync daemon host must be specified when using rsync daemon mode')

        if self.option('rsync-mode') == 'ssh' and not self.option('artifacts-host'):
            raise GlueError('artifacts host must be specified when using ssh mode')

    def source_destination_map(self) -> Any:
        source_destination_map = gluetool.utils.load_yaml(
            gluetool.utils.normalize_path(self.option('source-destination-map')),
        )

        additional_map_var = os.environ.get('SOURCE_DESTINATION_MAP', None)
        if additional_map_var:
            for entry in additional_map_var.split('#'):

                source, destination, permissions = entry.split(':')
                source_destination_map.append({
                    'source': source,
                    'destination': destination or '',
                    'permissions': permissions or None
                })

        # Render all templates in the map
        context = self.shared('eval_context')
        for dict_entry in source_destination_map:
            for key, value in dict_entry.items():
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
    def rsync_options(self) -> List[str]:

        options = gluetool.utils.normalize_multistring_option(self.option('rsync-options'))

        return [
            render_template(option, logger=self.logger, **self.shared('eval_context'))
            for option in options
        ]

    def create_archive_directory(self) -> None:
        request_id = self.shared('testing_farm_request').id

        cmd: List[str] = [
            'ssh',
            self.artifacts_host,
            'mkdir',
            '-p',
            '{}/{}'.format(self.artifacts_root, request_id)
        ]

        Command(cmd, logger=self.logger).run()

    def run_rsync(self, source: str, destination: str, options: Optional[List[str]] = None) -> None:
        options = options or []
        request_id = self.shared('testing_farm_request').id

        cmd = ['rsync']

        cmd += self.rsync_options

        if options:
            cmd += options

        cmd.append(source)

        if self.option('rsync-mode') == 'daemon':
            full_destination = 'rsync://{}/{}'.format(
                self.artifacts_rsync_host,
                os.path.join(request_id, destination)
            )
            cmd.append(full_destination)
            self.info('syncing {} to {}'.format(source, full_destination))

        else:
            full_destination = '{}:{}'.format(
                self.artifacts_host,
                os.path.join(self.artifacts_root, request_id, destination)
            )
            cmd.append(full_destination)
            self.info('syncing {} to {}'.format(source, full_destination))

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

    def archive(self) -> None:

        for entry in self.source_destination_map():

            if entry.get('source') is None:
                raise GlueError('Source path must be specified in source-destination-map')

            sources = entry['source']
            destination = entry.get('destination', '')
            permissions = entry.get('permissions', None)

            # If the entry['source'] is a wildcard, we need to use glob to find all the files
            for source in glob(sources):
                options = []

                if os.path.isdir(source):
                    options.append('--recursive')

                if permissions:
                    options.append('--chmod={}'.format(permissions))

                self.run_rsync(source, destination, options=options or None)

    def execute(self) -> None:
        if self.option('disable-archiving'):
            self.info('Archiving is disabled, skipping')
            return

        if self.option('rsync-mode') == 'ssh':
            self.create_archive_directory()

        if self.option('enable-parallel-archiving'):
            self.info('Starting parallel archiving')

            parallel_archiving_tick = self.option('parallel-archiving-tick')
            self._archive_timer = RepeatTimer(
                parallel_archiving_tick,
                self.archive
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

        self.archive()
