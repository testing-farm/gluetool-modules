# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os

import gluetool
import six

class HideSecrets(gluetool.Module):
    """
    Hide secrets from all files in the current dirctory.
    """

    name = 'hide-secrets'

    def destroy(self, failure=None):
        if not self.shared('user_secrets'):
            return

        self.info('hiding secrets from all logs')

        # TODO: this would really need shlex.quote or something to be safe
        sed_expr = ';'.join(
            's|{}|*****|g'.format(value)
            for _, value in six.iteritems(self.shared('user_secrets'))
        )

        os.system("find . -type f | xargs sed -i '{}'".format(sed_expr))
