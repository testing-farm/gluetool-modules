# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

# Note: avoid relative import of this git module instead of GitPython's git module
# https://www.python.org/dev/peps/pep-0328/
from __future__ import absolute_import

import os
import os.path
import stat
import tempfile
import re

import git

import gluetool.log
import gluetool.utils
from gluetool.utils import Result

from secret_type import Secret

# Type annotations
from typing import Any, Optional, List, Union  # noqa

# Clone directory for reproducer commands
TESTCODE_DIR = 'testcode'

# Regex for matching git clone url containing username and token
GIT_URL_REGEX = r"(https?:\/\/)(.+:.+)@(.+)"


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


class FailedToMerge(RemoteGitRepositoryError):
    pass


class SecretGitUrl(Secret[str]):
    """
    Container for storing Git URLs which might contain secret values. Extends ``secret_type.Secret`` with implementing
    conversion to ``str`` and ``bool`` for automatic censoring of credentials when logging the URL and usage in if
    statements.
    """

    def _hide_secrets(self, url: str) -> str:
        return re.sub(GIT_URL_REGEX, r"\1*****@\3", url)

    def __str__(self) -> str:
        return self._hide_secrets(self._dangerous_extract())

    def __repr__(self) -> str:
        return self._hide_secrets(self._dangerous_extract())

    def __bool__(self) -> bool:  # type: ignore  # Super-class expectes `SecretBool`
        return bool(self._dangerous_extract())


class RemoteGitRepository(gluetool.log.LoggerMixin):
    """
    A remote Git repository representation.

    :param gluetool.log.ContextLogger logger: Logger used for logging.
    :param SecretGitUrl clone_url: remote URL to use for cloning the repository. Note that git url is always treated as
        a secret value even when it does not contain any credentials.
    :param str branch: if set, it is the default branch to use in actions on the repository.
    :param path str: Initialize :py:class:`git.Git` instance from given path.
    :param str ref: if set, it it is the default point in repo history to manipulate.
    :param str web_url: if set, it is the URL of web frontend of the repository.
    :param list(str) clone_args: Additional arguments to pass to `git clone`.
    :param str merge: Git reference to merge into the currently checked out ref.
    """

    def __init__(
        self,
        logger: gluetool.log.ContextAdapter,
        clone_url: Optional[SecretGitUrl] = None,
        branch: Optional[str] = None,
        path: Optional[str] = None,
        ref: Optional[str] = None,
        web_url: Optional[str] = None,
        clone_args: Optional[List[str]] = None,
        merge: Optional[str] = None
    ) -> None:

        super(RemoteGitRepository, self).__init__(logger)

        self.clone_url = clone_url
        self.branch = branch
        self.ref = ref
        self.web_url = web_url
        self.path = path
        self.clone_args = clone_args or []
        self.merge = merge

        # holds git.Git instance, GitPython has no typing support
        self._instance: Any = None

        # list of commands use to clone the repository, note that git credentials were removed from each command
        # TODO: convert the type to `List[List[Union[str, SecretGitUrl]]]` if we need the credentials in the future
        self.commands: List[str] = []

        # initialize from given path if given
        if self.path:
            self.initialize_from_path(self.path)

    def __repr__(self) -> str:
        branch = self.branch or 'not specified'
        ref = self.ref or 'not specified'
        return '<RemoteGitRepository(clone_url={}, branch={}, ref={})>'.format(self.clone_url, branch, ref)

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
        clone_url: SecretGitUrl,
        path: str,
        shallow_clone: bool = True,
        ref: Optional[str] = None,
        clone_args: Optional[List[str]] = None,
    ) -> List[Union[str, SecretGitUrl]]:

        options: List[Union[str, SecretGitUrl]] = []
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
        clone_url: Optional[SecretGitUrl] = None,
        branch: Optional[str] = None,
        ref: Optional[str] = None,
        path: Optional[str] = None,
        prefix: Optional[str] = None,
        clone_args: Optional[List[str]] = None,
        merge: Optional[str] = None,
        clone_timeout: int = 120,
        clone_tick: int = 20
    ) -> str:
        """
        Clone remote repository and initialize :py:class:`git.Git` from it.

        :param gluetool.log.ContextAdapter logger: logger to use, default to instance logger.
        :param SecretGitUrl clone_url: remote URL to use for cloning the repository. If not set, the one specified
            during ``RemoteGitRepository`` initialization is used.
        :param str ref: checkout specified git ref. If not set, the one specified during ``RemoteGitRepository``
            initialization is used. If none of these was specified, top of the branch is checked out.
        :param str branch: checkout specified branch. If not set, the one specified during ``RemoteGitRepository``
            initialization is used.
        :param str path: if specified, clone into this path. Otherwise, a temporary directory is created.
        :param str prefix: if specified and `path` wasn't set, it is used as a prefix of directory created
            to hold the clone.
        :param list(str) clone_args: Additional arguments to pass to `git clone`.
        :param str merge: Git reference to merge into the currently checked out ref
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
        merge = merge or self.merge

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
            self._checkout_ref(clone_url, actual_path, ref)

        # If specified, merge ref into the currently checked out ref
        if merge:
            if merge != ref:
                self._merge(clone_url, merge, actual_path, logger)
            else:
                logger.warning('ref and merge are the same: {}. Skipping merging'.format(ref))

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
        clone_url: SecretGitUrl,
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

        options = self._get_clone_options(
            branch=branch, clone_url=clone_url, path=actual_path, ref=ref, clone_args=clone_args
        )
        # TODO: teach `gluetool.utils.Command` how to work with `secret_type.Secret[str]`
        cmd.options = [option._dangerous_extract()
                       if isinstance(option, SecretGitUrl)
                       else option
                       for option in options]
        self.commands.append(' '.join(map(str, ['git', 'clone'] + options)).replace(actual_path, TESTCODE_DIR))

        def _clone() -> Result[None, str]:

            # Log the 'git clone' command that's about to run. This log is used for unit tests too.
            logger.debug("{}".format(['git', 'clone'] + options))

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
                    # TODO: teach `gluetool.utils.Command` how to work with `secret_type.Secret[str]`
                    cmd.options = [option._dangerous_extract()
                                   if isinstance(option, SecretGitUrl)
                                   else option
                                   for option in self._get_clone_options(
                        branch=branch,
                        clone_url=clone_url,
                        path=actual_path,
                        shallow_clone=False,
                        ref=ref,
                        clone_args=clone_args
                    )]
                return Result.Error('Failed to clone git repository: {}, retrying'.format(exc.output.stderr))

            return Result.Ok(None)

        gluetool.utils.wait(
            "cloning with timeout {}s, tick {}s".format(clone_timeout, clone_tick),
            _clone,
            timeout=clone_timeout,
            tick=clone_tick
        )

    def _fetch_ref(self, clone_url: SecretGitUrl, actual_path: str, ref: str) -> str:
        """
        Fetches specified `ref` into the repo at `actual_path`.

        :param SecretGitUrl clone_url: remote URL to use for cloning the repository
        :param str actual_path: path to git repository which should be used for checkout
        :param str ref: git reference to fetch
        :returns: local git reference to which we checked out the requested ref
        :raises gluetool.GlueCommandError: if failed to fetch the reference
        """
        # we need to create a dedicated checkout ref, to mitigate collisions with existing refs
        checkout_ref = 'gluetool/{}'.format(ref)

        # Fetch the pull/merge request
        try:
            command: List[Union[str, SecretGitUrl]] = [
                'git',
                '-C', actual_path,
                'fetch',
                clone_url,
                '{}:{}'.format(ref, checkout_ref)
            ]

            self.commands.append(' '.join(map(str, command)).replace(actual_path, TESTCODE_DIR))
            # TODO: teach `gluetool.utils.Command` how to work with `secret_type.Secret[str]`
            gluetool.utils.Command(
                [c._dangerous_extract() if isinstance(c, SecretGitUrl) else c for c in command]
            ).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToFetchRef('Failed to fetch ref {}: {}'.format(ref, exc.output.stderr))

        return checkout_ref

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

    def _checkout_ref(self, clone_url: SecretGitUrl, actual_path: str, ref: str) -> None:
        """
        Checkout a git commit, reference, branch or tag.
        This method implements a general checkout which works for all cases.

        Note that checkouts for merge/pull requests require special handling for some of the git forges.

        GitLab: https://www.jvt.me/posts/2019/01/19/git-ref-gitlab-merge-requests/
        GitHub: https://www.jvt.me/posts/2019/01/19/git-ref-github-pull-requests/
        Pagure: https://docs.pagure.org/pagure/usage/pull_requests.html#working-with-pull-requests

        :param SecretGitUrl clone_url: remote URL to use for cloning the repository
        :param str actual_path: path to git repository which should be used for checkout
        :param str ref: git reference to checkout
        """

        merge_request_refspecs = [
            # GitLab
            '+refs/merge-requests/*:refs/remotes/origin/merge-requests/*',
            # GitHub & Pagure
            '+refs/pull/*:refs/remotes/origin/pull/*'
        ]

        # Enable merge request refs for supported git forges
        try:
            for refspec in merge_request_refspecs:
                command = [
                    'git',
                    '-C', actual_path,
                    'config',
                    '--add',
                    'remote.origin.fetch',
                    refspec
                ]

                self.commands.append(' '.join(command).replace(actual_path, TESTCODE_DIR))
                gluetool.utils.Command(command).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToConfigure(
                'Failed to configure git remote fetching on {}: {}'.format(ref, exc.output.stderr)
            )

        # Fetch the commit, in case it is from a merge/pull request ref
        checkout_ref = self._fetch_ref(clone_url, actual_path, ref)

        # Checkout the ref of the merge request
        try:
            command = [
                'git',
                '-C', actual_path,
                'checkout', checkout_ref
            ]

            self.commands.append(' '.join(command).replace(actual_path, TESTCODE_DIR))
            gluetool.utils.Command(command).run()

        except gluetool.GlueCommandError as exc:
            raise FailedToCheckoutRef('Failed to checkout ref {}: {}'.format(ref, exc.output.stderr))

    def _merge(self, clone_url: SecretGitUrl, ref: str, actual_path: str, logger: gluetool.log.ContextAdapter) -> None:
        """
        Merge specified ref into currently checked out ref.

        :param SecretGitUrl clone_url: remote URL to use for cloning the repository
        :param str ref: Git reference to merge into the currently checked out ref.
        :param str actual_path: path to git repository which should be used for checkout
        :param gluetool.log.ContextAdapter logger: Logger to use.
        """

        # Fetch the commit, in case it is from a merge/pull request ref
        merge_ref = self._fetch_ref(clone_url, actual_path, ref)

        logger.info('merging {}'.format(ref))
        command = gluetool.utils.Command(['git', '-C', actual_path, 'merge', '--no-edit', merge_ref], logger=logger)
        try:
            command.run()

        except gluetool.GlueCommandError as exc:
            raise FailedToMerge('Failed to merge {}: {}'.format(ref, exc.output.stderr)) from exc
        self.commands.append(' '.join(command.executable).replace(actual_path, TESTCODE_DIR))

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
