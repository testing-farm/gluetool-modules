# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import threading


class RepeatTimer(threading.Timer):
    """
    A repeated timer, which can be used as a drop-in replacement for py:class:`threading.Timer`.
    """

    def run(self) -> None:
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)
