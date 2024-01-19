# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os

import gluetool

from typing import Any, Optional, cast, List, Union  # noqa

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
    shared_functions = ['add_additional_secrets', 'hide_secrets']

    def add_additional_secrets(self, secret: Union[str, List[str]]) -> None:
        if isinstance(secret, list):
            self.additional_secrets.extend(secret)
        else:
            self.additional_secrets.append(secret)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(HideSecrets, self).__init__(*args, **kwargs)
        self.additional_secrets: List[str] = []

    def hide_secrets(self) -> None:
        testing_farm_request = cast(TestingFarmRequest, self.shared('testing_farm_request'))
        if not testing_farm_request:
            return

        secret_values = self.additional_secrets
        for environment in testing_farm_request.environments_requested:
            # hide environment.secrets
            if environment.secrets:
                secret_values += [secret_value for secret_value in environment.secrets.values() if secret_value]

        # POSIX.2 Basic Regular Expressions (BREs) have a specific set of characters
        # that you need to escape to use them as literals.
        #
        # Here's a list of special characters in POSIX.2 BREs:
        # * Backslash '\'
        # * Dot '.'
        # * Asterisk '*'
        # * Square brackets '[' and ']'
        # * Caret '^'
        # * Dollar sign '$'
        #
        # Note that backslash needs to be escaped with 4 backslashes for sed
        #
        # We need to also escape:
        # * Pipe '|' - because we use sed with '|' character
        # * Single quote ' - because we use it in the command
        def _posix_bre_escaped(value: str) -> str:
            value = value.replace('\\', '\\\\\\\\')
            for escape in r".*[]^$|'":
                value = value.replace(escape, r'\{}'.format(escape))
            return value

        # TFT-1339 - the value can be empty, make sure to skip it, nothing to hide there
        sed_expr = ';'.join('s|{}|*****|g'.format(_posix_bre_escaped(value)) for value in secret_values)

        # NOTE: We will deprecate this crazy module once TFT-1813
        if sed_expr:
            self.info("Hiding secrets from all files under '{}' path".format(self.option('search-path')))
            os.system("find '{}' -type f | xargs -I##### sed -i $'{}' '#####'".format(
                self.option('search-path'), sed_expr)
            )
        else:
            self.warn("No secrets to hide, all secrets had empty values")

    def destroy(self, failure: Optional[Any] = None) -> None:
        self.hide_secrets()
