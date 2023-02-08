# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re

import gluetool
import six

from typing import Any, Optional  # noqa


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
        # type: (Optional[Any]) -> None
        if not self.shared('user_secrets'):
            return

        # TODO: this would really need shlex.quote or something to be safe, all input data
        #       must be sanitized!
        # TFT-1339 - the value can be empty, make sure to skip it, nothing to hide there
        sed_expr = ';'.join(
            's|{}|*****|g'.format(re.escape(value))
            for _, value in six.iteritems(self.shared('user_secrets'))
            if value
        )

        if sed_expr:
            self.info("Hiding secrets from all files under '{}' path".format(self.option('search-path')))
            os.system("find '{}' -type f | xargs -n1 -I{{}} sed -i '{}' '{{}}'".format(
                self.option('search-path'), sed_expr)
            )
        else:
            self.warn("No secrets to hide, all secrets had empty values")
