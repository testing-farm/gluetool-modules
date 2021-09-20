# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os

import gluetool
import six

class HideSecrets(gluetool.Module):
    """
    Hide secrets from all files in the search path, by default
    current working directory.
    """

    name = 'hide-secrets'
    options = {
        'search-path': {
            'help': 'Path used to search for files (default: %(default)s)',
            'default': '.'
        }
    }

    def destroy(self, failure=None):
        if not self.shared('user_secrets'):
            return

        # TODO: this would really need shlex.quote or something to be safe
        sed_expr = ';'.join(
            's|{}|*****|g'.format(value)
            for _, value in six.iteritems(self.shared('user_secrets'))
        )

        self.info("Hiding secrets from all files under '{}' path".format(self.option('search-path')))
        os.system("find '{}' -type f | xargs sed -i '{}'".format(self.option('search-path'), sed_expr))
