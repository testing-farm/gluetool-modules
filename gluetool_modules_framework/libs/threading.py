# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import threading
import sys

from gluetool.log import Logging


class RepeatTimer(threading.Timer):
    """
    A repeated timer, which can be used as a drop-in replacement for py:class:`threading.Timer`.
    """

    def run(self) -> None:
        while not self.finished.wait(self.interval):
            try:
                self.function(*self.args, **self.kwargs)
            except Exception as exc:
                # Transient errors (e.g. API outages returning 504) should not terminate timer loop.
                Logging.get_logger().warning(
                    'Exception in RepeatTimer callback ({}). Retry on next tick'.format(exc),
                    exc_info=sys.exc_info(),
                    sentry=True
                )
