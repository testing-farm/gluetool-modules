# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

"""
Allow modules to inject enviroment variables via EnvInject module
"""

import gluetool
import six
from gluetool.log import format_dict
from typing import TYPE_CHECKING, List, Dict, Optional # noqa

DEFAULT_PROPS_FILE = 'envinject-citool.props'


class EnvInject(gluetool.Module):
    """
    Provides method for exporting variables which are then injected into job's
    env variables using Jenkins EnvInject plugin.
    """

    name = 'envinject'
    description = 'Allow other modules to add variables that EnvInject module applies when job finishes.'

    options = {
        ('f', 'file'): {
            'help': 'Properties file, read by EnvInject (default: %(default)s).',
            'default': DEFAULT_PROPS_FILE
        }
    }

    shared_functions = ['env']

    def __init__(self, glue: gluetool.Glue, name: str) -> None:
        super(EnvInject, self).__init__(glue, name)
        self._variables: Dict[str, str] = {}

    def env(self) -> Dict[str, str]:
        """
        Returns a dictionary whose content will be passed to EnvInject plugin.
        """

        return self._variables

    def destroy(self, failure: Optional[gluetool.Failure] = None) -> None:
        if not self.option('file'):
            self.debug('Do not save exported variables for EnvInject plugin: no file provided')
            return

        self.info('Saving exported variables for EnvInject plugin')
        self.debug('variables:\n{}'.format(format_dict(self._variables)))

        with open(self.option('file'), 'w') as f:
            for key, value in sorted(six.iteritems(self._variables)):
                f.write('{}="{}"\n'.format(key, value))
