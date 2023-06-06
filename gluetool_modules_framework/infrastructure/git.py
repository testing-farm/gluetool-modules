# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
import gluetool_modules_framework.libs
from gluetool_modules_framework.libs.git import RemoteGitRepository
from gluetool.utils import render_template, normalize_shell_option

from typing import Any, Dict, Optional, Union, List, TYPE_CHECKING, cast  # noqa


class Git(gluetool.Module):
    """
    Module provides details of a git repository. The repository is made available via the shared function
    ``git_repository`` or ``dist_git_repository``, which returns an instance of py:class:`RemoteGitRepository` class.
    """

    name = 'git'
    description = 'Provides a git repository.'
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = [
        ('General options', {
            'ref': {
                'help': """
                        Force git ref. Accepts also Jinja templates which will be rendered using
                        `eval_context` shared method.
                        """
            },
            'clone-url': {
                'help': """
                        Force git repository clone URL. Accepts also Jinja templates which will be rendered
                        using `eval_context` shared method.
                        """
            },
            'clone-args': {
                'help': 'Additional arguments to pass to clone command (default: none)',
                'action': 'append',
                'default': []
            },
            'merge': {
                'help': """
                        Optional ref to merge into the checked out ref. Accepts also Jinja templates which will be
                        rendered using `eval_context` shared method.
                        """
            }
        })
    ]

    required_options = ('clone-url',)

    shared_functions = ['git_repository', 'dist_git_repository']

    _repository: Optional[RemoteGitRepository] = None

    @property
    def clone_url(self) -> Optional[str]:
        option = self.option('clone-url')

        return render_template(option, **self.shared('eval_context'))

    @property
    def ref(self) -> Optional[str]:
        option = self.option('ref')
        if option is None:
            return option
        return render_template(option, **self.shared('eval_context'))

    @property
    def clone_args(self) -> List[str]:
        return normalize_shell_option(self.option('clone-args'))

    @property
    def merge(self) -> Optional[str]:
        option = self.option('merge')
        if option is None:
            return option
        return render_template(option, **self.shared('eval_context'))

    @property
    def eval_context(self) -> Dict[str, RemoteGitRepository]:
        __content__ = {  # noqa
            'GIT_REPOSITORY': """
                               git repository, represented as ``GitRepository`` instance.
                               """,
        }

        if not self._repository or gluetool_modules_framework.libs.is_recursion(__file__, 'eval_context'):
            return {}

        return {
            'GIT_REPOSITORY': self._repository,
        }

    def git_repository(self) -> Optional[RemoteGitRepository]:
        """
        Returns a git repository in the form of an instance
        of the py:class:`RemoteGitRepository` class.

        The module currently holds only one git repository and it caches it after the first retrieval
        in the execute function.

        :returns: instance of the :py:class:`RemoteGitRepository`
        """

        return self._repository

    # TODO: temporary method because other modules are calling `self.shared('dist_git_repository')`
    def dist_git_repository(self) -> Optional[RemoteGitRepository]:
        return self._repository

    def execute(self) -> None:
        self._repository = RemoteGitRepository(self.logger, clone_url=self.clone_url, ref=self.ref,
                                               clone_args=self.clone_args, merge=self.merge)
        self.info(str(self._repository))
