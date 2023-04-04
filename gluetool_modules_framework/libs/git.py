# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

# Note: avoid relative import of this git module instead of GitPython's git module
# https://www.python.org/dev/peps/pep-0328/
from __future__ import absolute_import

import os
import os.path
import stat
import tempfile

import git

import gluetool.log
import gluetool.utils
from gluetool.utils import Result

# Type annotations
from typing import cast, Any, Optional, List  # cast, Callable, Dict, List, NamedTuple, Optional, Tuple  # noqa

# Clone directory for reproducer commands
TESTCODE_DIR = 'testcode'


class RemoteGitRepositoryError(gluetool.GlueError):
    pass


class FailedToClone(RemoteGitRepositoryError):
    pass


class FailedToConfigure(RemoteGitRepositoryError):
    pass


class FailedToFetchRef(RemoteGitRepositoryError):
    pass


class FailedToCheckoutRef(RemoteGitRepositoryError):
    pass


class RemoteGitRepository(gluetool.log.LoggerMixin):
    """
    A remote Git repository representation.

    :param gluetool.log.ContextLogger logger: Logger used for logging.
    :param str clone_url: remote URL to use for cloning the repository.
    :param str branch: if set, it is the default branch to use in actions on the repository.
    :param path str: Initialize :py:class:`git.Git` instance from given path.
    :param str ref: if set, it it is the default point in repo history to manipulate.
    :param str web_url: if set, it is the URL of web frontend of the repository.
    """

    def __init__(
        self,
        logger: gluetool.log.ContextAdapter,
        clone_url: Optional[str] = None,
        branch: Optional[str] = None,
        path: Optional[str] = None,
        ref: Optional[str] = None,
        web_url: Optional[str] = None,
        clone_args: Optional[List[str]] = None
    ) -> None:

        super(RemoteGitRepository, self).__init__(logger)

        self.clone_url = clone_url
        self.branch = branch
        self.ref = ref
        self.web_url = web_url
        self.path = path
        self.clone_args = clone_args or []

        # holds git.Git instance, GitPython has no typing support
        self._instance: Any = None

        # list of commands use to clone the repository
        self.commands: List[str] = []

        # initialize from given path if given
        if self.path:
            self.initialize_from_path(self.path)

    def __repr__(self) -> str:
        clone_url = self.clone_url
        branch = self.branch or 'not specified'
        ref = self.ref or 'not specified'
        return '<RemoteGitRepository(clone_url={}, branch={}, ref={})>'.format(clone_url, branch, ref)

    @property
    def is_cloned(self) -> bool:
        """
        Repository is considered cloned if there is a git repository available on the local host
        and instance of :py:class:`git.Git` was initialized from it.
        """
        return bool(self._instance)

    @property
    def clonedir_prefix(self) -> str:
        # NOTE: this can be a ref, sanitize for paths - e.g. refs/merge-requests/15/head
        return 'git-{}'.format(self.branch or self.ref).replace('/', '-')

    def _get_clone_options(
        self,
        branch: Optional[str],
        clone_url: str,
        path: str,
        shallow_clone: bool = True,
        ref: Optional[str] = None,
        clone_args: Optional[List[str]] = None,
    ) -> List[str]:

        options = []
        options += clone_args or self.clone_args
        if not ref:
            assert branch
            if shallow_clone:
                options += ['--depth', '1']
            options += ['-b', branch]
        options += [clone_url, path]
        return options

    def clone(
        self,
        logger: Optional[gluetool.log.ContextAdapter] = None,
        clone_url: Optional[str] = None,
        branch: Optional[str] = None,
        ref: Optional[str] = None,
        path: Optional[str] = None,
        prefix: Optional[str] = None,
        clone_args: Optional[List[str]] = None,
        clone_timeout: int = 120,
        clone_tick: int = 20
    ) -> str:
        """
        Clone remote repository and initialize :py:class:`git.Git` from it.

        :param gluetool.log.ContextAdapter logger: logger to use, default to instance logger.
        :param str clone_url: remote URL to use for cloning the repository. If not set, the one specified during
            ``RemoteGitRepository`` initialization is used.
        :param str ref: checkout specified git ref. If not set, the one specified during ``RemoteGitRepository``
            initialization is used. If none of these was specified, top of the branch is checked out.
        :param str branch: checkout specified branch. If not set, the one specified during ``RemoteGitRepository``
            initialization is used.
        :param str path: if specified, clone into this path. Otherwise, a temporary directory is created.
        :param str prefix: if specified and `path` wasn't set, it is used as a prefix of directory created
            to hold the clone.
        :param list(str) clone_args: Additional arguments to pass to `git clone`.
        :param int clone_timeout: Timeout for `git clone` retries.
        :param int clone_tick: Delay in seconds before retrying `git clone`.
        :returns: path to the cloned repository. If `path` was given explicitly, it is returned as-is. Otherwise,
            function created a temporary directory and its path relative to CWD is returned.
        """

        # repository already initialized - nothing to clone
        if self._instance:
            if path and self.path != path:
                raise gluetool.GlueError('Clone path does not match initialized repository, misunderstood arguments?')

            assert self.path
            return self.path

        logger = logger or self.logger

        branch = branch or self.branch
        clone_url = clone_url or self.clone_url
        ref = ref or self.ref

        if branch and ref:
            raise gluetool.GlueError('Both ref and branch specified, misunderstood arguments?')

        if not branch and not ref:
            raise gluetool.GlueError('Neither ref nor branch specified, cannot continue')

        if not clone_url:
            raise gluetool.GlueError('No clone url specified, cannot continue')

        path = path or self.path
        original_path = path  # save the original path for later
        self.path = actual_path = self._generate_path(path=path, prefix=prefix)

        # NOTE: when we need to checkout a specific ref, we cannot do it directly with clone,
        # it requires another step - _checkout_ref()
        self._do_clone(
            logger=logger,
            clone_url=clone_url, branch=branch, actual_path=actual_path,
            clone_timeout=clone_timeout, clone_tick=clone_tick, ref=ref, clone_args=clone_args
        )

        # Make sure it's possible to enter this directory for other parties. We're not that concerned with privacy,
        # we'd rather let common users inside the repository when inspecting the pipeline artifacts. Therefore
        # setting clone directory permissions to ug=rwx,o=rx.
        self._set_clone_directory_permissions(actual_path=actual_path)

        # Handle checkout of a specific ref.
        # Refs starting with `refs/` are used for checking out merge requests and need special handling.
        # Otherwise fallback to checkout a general git ref.
        if ref:
            self._checkout_ref(actual_path, ref)

        self.commands.append('cd {}'.format(TESTCODE_DIR))

        # Since we used `dir` when creating repo directory, the path we have is absolute. That is not perfect,
        # we have an agreement with the rest of the world that we're living in current directory, which we consider
        # a workdir (yes, it would be better to have an option to specify it explicitly), we should get the relative
        # path instead.
        # This applies to path *we* generated only - if we were given a path, we won't touch it.
        path = actual_path if original_path else os.path.relpath(actual_path, os.getcwd())
        self.initialize_from_path(path)

        return actual_path

    def _do_clone(
        self,
        logger: gluetool.log.ContextAdapter,
        clone_url: str,
        branch: Optional[str],
        actual_path: str,
        clone_timeout: int,
        clone_tick: int,
        ref: Optional[str] = None,
        clone_args: Optional[List[str]] = None
    ) -> None:

        # TODO: it would be nice to be able to use the `self.__repr__` method but it is actually not correct using it
        # here, values such `branch` and `ref` can be different than the ones printed in `self.__repr__`
        logger.info('cloning repo {} (branch {}, ref {})'.format(
            clone_url,
            branch or 'not specified',
            ref or 'not specified'
        ))

        cmd = gluetool.utils.Command(['git', 'clone'], logger=logger)

        cmd.options = self._get_clone_options(
            branch=branch, clone_url=clone_url, path=actual_path, ref=ref, clone_args=clone_args
        )

        reproducer_command = ['git', 'clone'] + cmd.options
        self.commands.append(' '.join(reproducer_command).replace(actual_path, TESTCODE_DIR))

        def _clone() -> Result[None, str]:

            # Log the 'git clone' command that's about to run. This log is used for unit tests too.
            logger.debug("{}".format(cmd.executable + cmd.options))

            try:
                cmd.run()

            except gluetool.GlueCommandError as exc:
                # Some git servers do not support shallow cloning over http(s)
                # Retry without shallow clone in the next try

                # Asserts are required here, see
                # https://mypy.readthedocs.io/en/latest/common_issues.html#narrowing-and-inner-functions
                assert branch or ref
                assert clone_url
                if exc.output.stderr is not None and \
                        'dumb http transport does not support shallow capabilities' in exc.output.stderr:
                    cmd.options = self._get_clone_options(
                        branch=branch,
                        clone_url=clone_url,
                        path=actual_path,
                        shallow_clone=False,
                        ref=ref,
                        clone_args=clone_args
                    )
                return Result.Error('Failed to clone git repository: {}, retrying'.format(exc.output.stderr))

            return Result.Ok(None)

        gluetool.utils.wait(
            "cloning with timeout {}s, tick {}s".format(clone_timeout, clone_tick),
            _clone,
            timeout=clone_timeout,
            tick=clone_tick
        )

    def _set_clone_directory_permissions(
        self,
        actual_path: str
    ) -> None:
        os.chmod(
            actual_path,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH  # noqa: E501  # line too long
        )

    def _generate_path(self, path: Optional[str] = None,  prefix: Optional[str] = None) -> str:
        if path:
            return path

        elif prefix:
            return tempfile.mkdtemp(dir=os.getcwd(), prefix=prefix)

        return tempfile.mkdtemp(dir=os.getcwd())

    def _checkout_merge_request_ref(self, actual_path: str, ref: str) -> None:
        """
        Checkout git reference for merge/pull requests.
        These require special handling for some of the git forges.

        GitLab: https://www.jvt.me/posts/2019/01/19/git-ref-gitlab-merge-requests/

        GitHub: https://www.jvt.me/posts/2019/01/19/git-ref-github-pull-requests/

        Pagure: https://docs.pagure.org/pagure/usage/pull_requests.html#working-with-pull-requests

        Currently, this way are handled all refs which start with the `refs/` string.

        :param str actual_path: path to git repository which should be used for checkout
        :param str ref: git reference to checkout
        """

        # Enable merge request refs for GitLab
        try:
            command = [
                'git',
                '-C', actual_path,
                'config',
                'remote.origin.fetch',
                '+refs/merge-requests/*:refs/remotes/origin/merge-requests/*'
            ]

            self.commands.append(' '.join(command).replace(actual_path, TESTCODE_DIR))
            gluetool.utils.Command(command).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToConfigure(
                'Failed to configure git remote fetching on {}: {}'.format(ref, exc.output.stderr)
            )

        assert self.clone_url

        # Fetch the pull/merge request
        try:
            command = [
                'git',
                '-C', actual_path,
                'fetch',
                self.clone_url,
                '{}:{}'.format(ref, ref)
            ]

            self.commands.append(' '.join(command).replace(actual_path, TESTCODE_DIR))
            gluetool.utils.Command(command).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToFetchRef('Failed to fetch ref {}: {}'.format(ref, exc.output.stderr))

        # Checkout the ref of the merge request
        try:
            command = [
                'git',
                '-C', actual_path,
                'checkout', ref
            ]

            self.commands.append(' '.join(command).replace(actual_path, TESTCODE_DIR))
            gluetool.utils.Command(command).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToCheckoutRef('Failed to checkout branch {}: {}'.format(ref, exc.output.stderr))

    def _checkout_ref(self, actual_path: str, ref: str) -> None:

        if ref.startswith('refs/'):
            self._checkout_merge_request_ref(actual_path, ref)
        else:
            self._checkout_general_ref(actual_path, ref)

    def _checkout_general_ref(self, actual_path: str, ref: str) -> None:
        """
        Checkout git reference.
        The reference can be a branch, tag or git SHA.
        The function first tries to resolve the reference to a git SHA.
        And then uses the git SHA to checkout to a branch called `testbranch`.

        :param str actual_path: path to git repository which should be used for checkout
        :param str ref: the git reference to checkout
        """

        # Default branch name of a checkout via hash, we always checkout a "named" branch
        branch_name = ref[:8]

        # Find out if the given ref is a reference and find its hash
        try:
            command = [
                'git',
                '-C', actual_path,
                'show-ref', '-s', ref
            ]

            show_ref_output = gluetool.utils.Command(command).run()

            # As the branch name us the reference name
            branch_name = '{}-testing-farm-checkout'.format(ref)

            assert show_ref_output.stdout
            ref = show_ref_output.stdout.split()[0].rstrip()

        except gluetool.GlueCommandError:
            pass

        try:
            command = [
                'git',
                '-C', actual_path,
                'checkout', '-b', branch_name, ref
            ]

            reproducer_command = [
                'git',
                '-C', actual_path,
                'checkout', '-b', 'testbranch', ref
            ]

            self.commands.append(' '.join(reproducer_command).replace(actual_path, TESTCODE_DIR))

            gluetool.utils.Command(command).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToCheckoutRef('Failed to checkout ref {}: {}'.format(ref, exc.output.stderr))

    def initialize_from_path(self, path: str) -> Any:
        """
        Initialize a :py:class:`git.Git` instance from given path.

        :param str path: path to git repository which should be used for initialization
        :raises gluetool.glue.GlueError: if failed to initialize instance from the given path
        """
        try:
            self._instance = git.Git(path)
        except git.exc.GitError as error:
            raise gluetool.GlueError("Failed to initialize git repository from path '{}': {}".format(path, error))

    def gitlog(self, *args: str) -> Any:
        """
        Return git log according to given parameters. Note that we cannot call this method `log` as it would
        collide with method `log` of our parent :py:class:`gluetool.log.LoggerMixin`
        """
        gluetool.utils.log_dict(self.debug, 'git log args', args)

        log = self._instance.log(*args)

        gluetool.utils.log_blob(self.debug, 'logs found', log)

        return log
