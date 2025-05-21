# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
from contextlib import nullcontext

import psutil
import six
from gluetool import Failure, Module
from gluetool.log import log_dict
from gluetool.utils import cached_property, normalize_bool_option, normalize_multistring_option
from gluetool_modules_framework.libs.threading import RepeatTimer

# Type annotations
# pylint: disable=unused-import,wrong-import-order
from typing import Any, Dict, List, Optional  # noqa

# Default tick in seconds between checks for OOM
DEFAULT_OOM_CHECK_TICK = 5


class OutOfMemory(Module):
    """
    Handle out-of-memory events for the pipeline.

    Checks memory consumption by counting the RSS memory of all processes if running in a container,
    except toolbox container.

    If the process is running outside of a container or in a toolbox container, the check
    is disabled, because there is no easy way to identify containers launched by ``tmt``.

    If the ``reservation`` memory limit is reached, it sends a warning to Sentry.

    If the memory ``limit`` is reached, it terminates the terminates the pipeline.
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
                "count-once": {
                    "help": """
                        List of process names counted only once, maximum RSS memory.
                        Matches as substrings in the process command line.
                        Used to mitigate multiplication of memory usage for tools using multiple
                        processes and shared memory.
                    """,
                    "action": "append"
                }
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
                "force": {
                    "help": "Force running, even if not running in a container. (default: %(default)s).",
                    "action": "store_true",
                    "default": "no"
                },
                "tick": {
                    "help": """
                        Number of seconds to wait between checking memory consumption again. (default: %(default)s)
                    """,
                    "type": int,
                    "default": DEFAULT_OOM_CHECK_TICK,
                },
                "verbose-logging": {
                    "help": "Enable verbose logging for memory checking. (default: %(default)s",
                    "action": "store_true",
                    "default": "no"
                },
                "print-usage-only": {
                    "help": "Only print the usage and do not start out-of-memory monitoring. (default: %(default)s",
                    "action": "store_true",
                    "default": "no"
                }
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

    @cached_property
    def available(self) -> bool:
        if os.path.exists('/run/.containerenv') and not os.path.exists('/run/.toolboxenv'):
            return True
        return False

    @cached_property
    def enabled(self) -> bool:
        return normalize_bool_option(self.option('enabled'))

    @cached_property
    def verbose_logging(self) -> bool:
        return normalize_bool_option(self.option('verbose-logging'))

    @cached_property
    def force(self) -> bool:
        return normalize_bool_option(self.option('force'))

    @cached_property
    def count_once(self) -> List[str]:
        return normalize_multistring_option(self.option('count-once'))

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
        self.debug('Stopping out-of-memory monitoring')
        if self._oom_timer:
            self._oom_timer.cancel()
            self._oom_timer = None

        # cancel the pipeline
        psutil.Process().terminate()

    def total_rss_memory(self) -> int:
        """
        Get sum of RSS memory of all available processes.
        """
        total_rss = 0

        rss_max: Dict[str, int] = {
            cmd: 0
            for cmd in self.count_once
        }

        for proc in psutil.process_iter():
            try:
                cmdline = " ".join(proc.cmdline())
                rss = proc.memory_info().rss

                # count certain processes only once, maximum value
                # used to mitigate counting multiple processes with shared memory
                for cmd in self.count_once:
                    if cmd in cmdline:
                        rss_max[cmd] = max(rss_max[cmd], rss)
                        break
                else:
                    total_rss += rss

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # process gone or inaccessible, skip it
                if self.verbose_logging:
                    self.debug("Ignoring process '{}', it is gone or inacessible".format(proc.pid))
                continue

        # add processes counted only once
        total_rss += sum(rss for rss in six.itervalues(rss_max))

        return total_rss

    def handle_oom(self) -> None:
        """
        Handle out-of-memory events.
        """

        reservation = self.option('reservation')
        limit = self.option('limit')

        memory_consumed = self.total_rss_memory() / 1024**2

        if self.verbose_logging:
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
        if not self.enabled:
            self.info('Out-of-memory monitoring is disabled')
            return

        if not self.available and not self.force:
            self.warn(
                "Out-of-memory monitoring is unavailable, not running in a container"
            )
            return

        log_dict(
            self.info,
            'Starting out-of-memory monitoring, check every {} seconds'.format(self.option('tick')),
            {
                'reserved': '{} MiB'.format(self.option('reservation')),
                'limit': '{} MiB'.format(self.option('limit'))
            }
        )

        if normalize_bool_option(self.option("print-usage-only")):
            self.info("Detected memory usage: {:.2f} MiB".format(self.total_rss_memory() / 1024**2))
            return

        self._oom_timer = RepeatTimer(
            self.option('tick'), self.handle_oom
        )
        self._oom_timer.start()
