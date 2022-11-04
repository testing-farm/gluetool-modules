# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import importlib
try:
    import ipdb
except ImportError:
    pass
import gluetool

from typing import Optional


class Debug(gluetool.Module):
    """
    Break into ipdb debugger during pipeline execution.

    By default the debugger is only run in case the pipeline enters
    a failure. This can be useful to peek at the pipeline state
    and debug.

    You can force breaking into the debugger during module execute
    via the `--execute` option.

    Likewise, in case you would like to break into the debugger during
    the destroy function, use the `--destroy` option.
    """
    name = 'debug'
    description = 'Break into ipdb debugger during pipeline execution.'

    options = {
        'execute': {
            'help': 'Break into debugger during module `execute` function',
            'action': 'store_true'
        },
        'destroy': {
            'help': 'Break into debugger during module `destroy` function',
            'action': 'store_true'
        }
    }

    available = True

    def sanity(self):
        if importlib.util.find_spec('ipdb') is None:
            self.available = False
            raise gluetool.GlueError("Install 'development' extras to use this module")

    def _break(self):
        # type: () -> None
        self.warn('Dropping into ipdb debug shell')
        ipdb.set_trace()

    def execute(self):
        # type: () -> None
        if self.option('execute'):
            self._break()

    def destroy(self, failure=None):
        # type: (Optional[gluetool.Failure]) -> None
        if self.available and (self.option('destroy') or failure):
            self._break()
