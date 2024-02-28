# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import tempfile

import gluetool

from typing import Any, Optional, List, Set, Union  # noqa


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
    shared_functions = ['add_secrets', 'hide_secrets']

    def add_secrets(self, secret: Union[str, List[str]]) -> None:
        if isinstance(secret, list):
            self._secrets.update(secret)
        else:
            self._secrets.add(secret)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(HideSecrets, self).__init__(*args, **kwargs)
        self._secrets: Set[str] = set()

    def hide_secrets(self, search_path: Optional[str] = None) -> None:
        search_path = search_path or self.option('search-path')
        assert search_path

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
        # Note that backslash needs to be escaped separately
        #
        # We need to also escape:
        # * Pipe '|' - because we use sed with '|' character
        def _posix_bre_escaped(value: str) -> str:
            value = value.replace('\\', '\\\\')
            for escape in r".*[]^$|":
                value = value.replace(escape, r'\{}'.format(escape))
            return value

        sed_expr = '\n'.join('s|{}|*****|g'.format(_posix_bre_escaped(value)) for value in self._secrets)

        # NOTE: We will deprecate this crazy module once TFT-1813
        if not sed_expr:
            self.debug("No secrets to hide, all secrets had empty values")
            return

        self.debug("Hiding secrets from all files under '{}' path".format(search_path))

        with tempfile.NamedTemporaryFile(mode='w', dir='.') as temp:
            temp.write(sed_expr)
            temp.flush()
            command = "find '{}' -type f | xargs -i##### sed -i -f '{}' '#####'".format(search_path, temp.name)

            output = gluetool.utils.Command([command]).run(shell=True)

        output.log(self.logger)

        if output.exit_code != 0:
            gluetool.GlueError('Failed to hide secrets, secrets could be leaked!')

        # Be paranoic that modified files were written to the disk
        gluetool.utils.Command(['sync', search_path]).run()

    def destroy(self, failure: Optional[Any] = None) -> None:
        self.hide_secrets()
