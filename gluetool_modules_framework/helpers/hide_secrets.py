# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re

import gluetool

from typing import Any, Optional, cast, List  # noqa

from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest


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
        testing_farm_request = cast(TestingFarmRequest, self.shared('testing_farm_request'))
        if not testing_farm_request:
            return

        secret_values = []  # type: List[str]
        for environment in testing_farm_request.environments_requested:
            if environment.secrets:
                secret_values += [secret_value for secret_value in environment.secrets.values() if secret_value]

        # TODO: this would really need shlex.quote or something to be safe, all input data
        #       must be sanitized!
        # TFT-1339 - the value can be empty, make sure to skip it, nothing to hide there
        sed_expr = ';'.join('s|{}|*****|g'.format(re.escape(value)) for value in secret_values)

        if sed_expr:
            self.info("Hiding secrets from all files under '{}' path".format(self.option('search-path')))
            os.system("find '{}' -type f | xargs -n1 -I{{}} sed -i '{}' '{{}}'".format(
                self.option('search-path'), sed_expr)
            )
        else:
            self.warn("No secrets to hide, all secrets had empty values")
