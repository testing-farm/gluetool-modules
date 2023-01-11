# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
import gluetool_modules_framework.libs
from gluetool_modules_framework.libs.git import RemoteGitRepository
from gluetool.utils import render_template

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
            }
        })
    ]

    shared_functions = ['git_repository', 'dist_git_repository']

    _repository = None  # type: Optional[RemoteGitRepository]

    @property
    def clone_url(self):
        # type: () -> Optional[str]
        option = self.option('clone-url')
        if option is None:
            return option
        return render_template(option, **self.shared('eval_context'))

    @property
    def ref(self):
        # type: () -> Optional[str]
        option = self.option('ref')
        if option is None:
            return option
        return render_template(option, **self.shared('eval_context'))

    @property
    def eval_context(self):
        # type: () -> Dict[str, RemoteGitRepository]
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

    def git_repository(self):
        # type: () -> Optional[RemoteGitRepository]
        """
        Returns a git repository in the form of an instance
        of the py:class:`RemoteGitRepository` class.

        The module currently holds only one git repository and it caches it after the first retrieval
        in the execute function.

        :returns: instance of the :py:class:`RemoteGitRepository`
        """

        return self._repository

    # TODO: temporary method because other modules are calling `self.shared('dist_git_repository')`
    def dist_git_repository(self):
        # type: () -> Optional[RemoteGitRepository]
        return self._repository

    def execute(self):
        # type: () -> None
        self._repository = RemoteGitRepository(self.logger, clone_url=self.clone_url, ref=self.ref)

        self.info(str(self._repository))
