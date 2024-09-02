# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import tempfile

import gluetool

from gluetool.result import Result

from typing import Any, Optional, List, Set, Union  # noqa

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10


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
        },
        'retry-tick': {
            'help': 'Timeout between retries for failed operations. (default: %(default)s)',
            'metavar': 'RETRY_TICK',
            'type': int,
            'default': DEFAULT_RETRY_TICK,
        },
        'retry-timeout': {
            'help': 'Timeout for retries in seconds. (default: %(default)s)',
            'metavar': 'RETRY_TIMEOUT',
            'type': int,
            'default': DEFAULT_RETRY_TIMEOUT,
        },
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
            value = value.replace('\n', '\\n')
            for escape in r".*[]^$|":
                value = value.replace(escape, r'\{}'.format(escape))
            return value

        sed_expr = '\n'.join('s|{}|hidden|g'.format(_posix_bre_escaped(value)) for value in self._secrets)

        # NOTE: We will deprecate this crazy module once TFT-1813
        if not sed_expr:
            self.debug("No secrets to hide, all secrets had empty values")
            return

        self.debug("Hiding secrets from all files under '{}' path".format(search_path))

        with tempfile.NamedTemporaryFile(mode='w', dir='.') as temp:
            temp.write(sed_expr)
            temp.flush()
            command = "find '{}' -type f | xargs -i##### sed -i -f '{}' '#####'".format(search_path, temp.name)

            def _run_sed() -> Result[bool, bool]:
                try:
                    output = gluetool.utils.Command([command], logger=self.logger).run(shell=True)
                except gluetool.GlueCommandError as exc:
                    self.warn('sync command "{}" failed, retrying: {}'.format(command, exc), sentry=True)
                    return Result.Error(False)

                output.log(self.logger)
                if output.exit_code != 0:
                    raise gluetool.GlueError('Failed to hide secrets, secrets could be leaked!')

                return Result.Ok(True)

            gluetool.utils.wait(
                "Running '{}'".format(command),
                _run_sed,
                timeout=self.option('retry-timeout'),
                tick=self.option('retry-tick')
            )

        # Be paranoic that modified files were written to the disk
        cmd = ['sync', search_path]

        def _run_sync() -> Result[bool, bool]:
            try:
                gluetool.utils.Command(cmd, logger=self.logger).run()
            except gluetool.GlueCommandError as exc:
                self.warn('sync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc), sentry=True)
                return Result.Error(False)
            return Result.Ok(True)

        try:
            gluetool.utils.wait(
                "sync of '{}'".format(search_path),
                _run_sync,
                timeout=self.option('retry-timeout'),
                tick=self.option('retry-tick')
            )

        except gluetool.GlueError:
            self.warn('Failed to sync modified files to disk.', sentry=True)

    def destroy(self, failure: Optional[Any] = None) -> None:
        self.hide_secrets()
