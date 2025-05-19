# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
from contextlib import nullcontext

import psutil
from gluetool import Failure, Module
from gluetool.log import log_dict
from gluetool.utils import normalize_bool_option
from gluetool_modules_framework.libs.threading import RepeatTimer

# Type annotations
# pylint: disable=unused-import,wrong-import-order
from typing import Any, Optional  # noqa

# Default tick in seconds between checks for OOM
DEFAULT_OOM_CHECK_TICK = 5

# File containing the current memory consumption
DEFAULT_CGROUPS_MEMORY_CURRENT_PATH = "/sys/fs/cgroup/memory.current"


class OutOfMemory(Module):
    """
    Handle out-of-memory events for the pipeline.

    Checks memory consumption via cgroups v2 memory controller.

    If the ``reservation`` memory limit is reached, it sends a warning to Sentry.

    If the memory ``limit`` is reached, it terminates the terminates the pipeline.

    The module will work only if run in a container engine using cgroups v2, where the
    memory consumption can be detected by reading ``/sys/fs/cgroup/memory.current`` file.
    If the file does not exist, the module just emits a warning and does nothing.
    """

    name = "oom"
    description = "Handle out-of-memory events for the pipeline."

    options = [
        (
            "Memory",
            {
                "reservation": {
                    "help": "The expected memory reservation in MiB. Emits a single warning to Sentry once breached.",
                    "type": int,
                    "metavar": "MiB",
                },
                "limit": {
                    "help": "The maximum memory consumption in MiB. If breached the pipeline is cancelled.",
                    "type": int,
                    "metavar": "MiB",
                },
                "monitoring-path": {
                    "help": "The file used to read current memory consumption in bytes. (default: %(default)s).",
                    "default": DEFAULT_CGROUPS_MEMORY_CURRENT_PATH,
                },
            },
        ),
        (
            "General",
            {
                "enabled": {
                    "help": "Enable out-of-memory checking. (default: %(default)s).",
                    "action": "store_true",
                    "default": "yes"
                },
                "tick": {
                    "help": """
                        Number of seconds to wait between checking memory consumption again. (default: %(default)s)
                    """,
                    "type": int,
                    "default": DEFAULT_OOM_CHECK_TICK,
                },
                "verbose": {
                    "help": "Enable verbose logging for memory checking.",
                    "action": "store_true",
                },
            },
        ),
    ]

    required_options = ('reservation', 'limit')
    shared_functions = ['oom_message']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(OutOfMemory, self).__init__(*args, **kwargs)
        self._oom_timer: Optional[RepeatTimer] = None
        self._reservation_reached: bool = False
        self._oom_message: Optional[str] = None

    def oom_message(self) -> Optional[str]:
        """
        Use this shared function to find out if the pipeline was terminated due to out-of-memory event.

        The function return `None` if there was no event, otherwise it returns the event error message.
        """
        return self._oom_message

    def terminate_pipeline(self) -> None:
        """
        Terminate the pipeline by terminating the current process.
        """
        self.error(
            'Terminating pipeline because worker is out of memory, more than {} MiB consumed.'.format(
                self.option('limit')
            ),
            sentry=True
        )

        self._oom_message = "Worker out-of-memory, more than {} MiB consumed.".format(self.option('limit'))

        # stop the repeat timer, it will not be needed anymore
        self.debug('Stopping pipeline oom check')
        if self._oom_timer:
            self._oom_timer.cancel()
            self._oom_timer = None

        # cancel the pipeline
        psutil.Process().terminate()

    def handle_oom(self) -> None:
        """
        Handle out-of-memory events.
        """

        reservation = self.option('reservation')
        limit = self.option('limit')

        with open(self.option('monitoring-path')) as f:
            memory_consumed = int(f.read()) / 1024**2

        if self.option('verbose'):
            log_dict(self.debug, 'out-of-memory check', {
                'memory': '{:.2f} MiB'.format(memory_consumed),
                'reserved': '{} MiB'.format(reservation),
                'limit': '{} MiB'.format(limit)
            })

        if not self._reservation_reached and memory_consumed > reservation:
            self.warn(
                "Reservation memory {} MiB reached".format(reservation),
                sentry=True
            )
            self._reservation_reached = True

        if memory_consumed > limit:
            with self.shared('pipeline_cancellation_lock') or nullcontext():
                self.terminate_pipeline()

    def destroy(self, failure: Optional[Failure] = None) -> None:
        # stop the repeat timer, it will not be needed anymore
        if self._oom_timer:
            self.info('Stopping out-of-memory monitoring')
            self._oom_timer.cancel()
            self._oom_timer = None

    def execute(self) -> None:
        if not normalize_bool_option(self.option('enabled')):
            self.info('Out-of-memory monitoring is disabled')
            return

        monitoring_path = self.option('monitoring-path')

        if not os.path.exists(monitoring_path):
            self.warn(
                "Out-of-memory monitoring is unavailable, '{}' not available".format(monitoring_path)
            )
            return

        log_dict(
            self.info,
            'Starting out-of-memory monitoring, check every {} seconds'.format(self.option('tick')),
            {
                'reserved': '{} MiB'.format(self.option('reservation')),
                'limit': '{} MiB'.format(self.option('limit')),
                'monitoring': monitoring_path
            }
        )

        self._oom_timer = RepeatTimer(
            self.option('tick'), self.handle_oom
        )
        self._oom_timer.start()
