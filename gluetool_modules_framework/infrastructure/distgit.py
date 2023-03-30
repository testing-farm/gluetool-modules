# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import re
import six

import gluetool

from gluetool.utils import cached_property, IncompatibleOptionsError, normalize_path, normalize_shell_option, \
        PatternMap, render_template
from gluetool.log import log_blob, log_dict

import gluetool_modules_framework.libs
import gluetool_modules_framework.libs.git

# Type annotations
from typing import Any, Dict, List, Optional, Set, Union, TYPE_CHECKING, cast  # noqa

if TYPE_CHECKING:
    from gluetool.utils import Pattern
    from gluetool_modules_framework.infrastructure.koji_fedora import KojiTask
    from gluetool.log import ContextAdapter


class DistGitRepository(gluetool_modules_framework.libs.git.RemoteGitRepository):
    """
    Provides a dist-git repository.
    """

    def __init__(self, logger: ContextAdapter, package: str, **kwargs: Any) -> None:
        self.package = package

        super(DistGitRepository, self).__init__(logger, **kwargs)

    def __repr__(self) -> str:
        return '<DistGitRepository(package="{}", branch="{}")>'.format(self.package, self.branch)

    @cached_property
    def ci_config_url(self) -> str:
        """
        To check for CI configuration we simply check if fmf metadata are present. We want to avoid
        the need to clone the dist-git repository.
        """

        # NOTE: url for Pagure instances, move to config later ideally
        # return '{}/raw/{}/f/.fmf/version'.format(self.web_url, self.ref if self.ref else self.branch)

        # NOTE: url for cgit instance
        return '{}/plain/.fmf/version?id={}'.format(self.web_url, self.ref if self.ref else self.branch)

    @cached_property
    def sti_tests_url(self) -> str:
        """
        URL of STI tests.
        """

        # Currently we check only tests/ folder, which should be a pretty solid indication of STI tests.
        # The STI tests can be tests/tests*.yml, which is a bit hard to check via URL as we would need to parse html.

        # NOTE: url for Pagure instances, move to config later ideally
        # return '{}/blob/{}/f/tests'.format(self.web_url, self.ref if self.ref else self.branch)

        # NOTE: url for cgit instance
        return '{}/tree/tests?id={}'.format(self.web_url, self.ref if self.ref else self.branch)

    @cached_property
    def rpminspect_yaml_url(self) -> str:
        """
        URL of rpminspect.conf file.
        """

        return '{}/tree/rpminspect.yaml?id={}'.format(self.web_url, self.ref if self.ref else self.branch)

    @cached_property
    def gating_config_url(self) -> str:
        # NOTE: url for Pagure instances, move to config later ideally
        # return '{}/raw/{}/f/gating.yaml'.format(self.web_url, self.ref if self.ref else self.branch)

        # NOTE: url for cgit instance
        return '{}/plain/gating.yaml?id={}'.format(self.web_url, self.ref if self.ref else self.branch)

    def _get_url(self, url: str, success_message: str, failure_message: str) -> Optional[str]:
        with gluetool.utils.requests() as request:
            response = request.get(url)

        if response.status_code == 200:
            self.info(success_message)

            return cast(str, response.text)

        self.info(failure_message)

        return None

    @cached_property
    def has_ci_config(self) -> bool:
        """
        Indicates if CI configuration present in dist-git by checking for `.fmf/version` file.

        :returns: ``True`` when dist-git repository contains CI configuration, ``False`` otherwise.
        """

        return bool(self._get_url(self.ci_config_url, 'contains CI configuration', 'does not contain CI configuration'))

    @cached_property
    def _sti_tests_folder(self) -> Optional[str]:
        """
        STI tests folder, not interesting for the user, so keeping internal.
        """

        return self._get_url(self.sti_tests_url, 'has STI tests', 'does not have STI tests')

    @cached_property
    def _gating_config_response(self) -> Any:
        with gluetool.utils.requests() as request:
            response = request.get(self.gating_config_url)

        if response.status_code == 200:
            log_blob(self.info, "gating configuration '{}'".format(self.gating_config_url), response.content)

            return response

        self.info("dist-git repository has no gating.yaml '{}'".format(self.gating_config_url))

        return None

    @cached_property
    def has_sti_tests(self) -> bool:
        """
        :returns: ``True`` when dist-git repository contains Standard Test Interface (STI) tests, ``False`` otherwise.
        """

        return bool(self._sti_tests_folder)

    @cached_property
    def has_gating(self) -> bool:
        """
        :returns: True if dist-git repository has gating enabled, False otherwise
        """
        return bool(self._gating_config_response)

    @cached_property
    def gating_recipients(self) -> List[str]:
        """
        Returns list of recipients specified in a comment in gating.yaml file as a list. Here
        is an example of gating yaml with the recipients in an comment:

        .. code-block:: yaml

           ---

           # recipients: batman, robin
           product_versions:
           - rhel-8
           decision_context: osci_compose_gate
           rules:
           - !PassingTestCaseRule {test_case_name: baseos-ci.brew-build.tier1.functional}

        :returns: List of recipients form gating.yaml provided via comment in the gating.yaml file.
        """
        response = self._gating_config_response

        if not response or 'recipients:' not in response.content:
            return []

        return [
            recipient.strip() for recipients in re.findall("recipients:.*", response.content, re.MULTILINE)
            for recipient in recipients.lstrip("recipients:").split(',')
        ]

    @cached_property
    def rpminspect_yaml(self) -> Optional[str]:
        """
        Returns contents of rpminspect.yaml file. This file can be placed in the dist-git repository
        to customize rpminspect execution.

        :returns: Contents of the rpminspect.yaml file.
        """

        return self._get_url(
            self.rpminspect_yaml_url,
            'contains rpminspect configuration',
            'does not contain rpminspect configuration'
        )


class DistGit(gluetool.Module):
    """
    Module provides details of a dist-git repository. The repository is made available via the shared
    function ``dist_git_repository``, which returns an instance of py:class:`DistGitRepository` class.

    The module supports currently one method for resolving the dist-git repository details:

    * ``artifact``: Resolve dist-git repository for the primary artifact in the pipeline. If some of the options
                    ``branch``, ``ref``, ``web-url`` or ``clone-url`` are specified they override the resolved values.

    The shared function ``dist_git_bugs`` finds all the bugs mentioned in commit logs from a previous version
    of the package. The previous version of the package is provided via primary task's ``baseline`` property. See
    help of the module which provides ``primary_task`` shared function in the pipeline for more information.

    When ``dist_git_bugs`` shared function is used the module clones the dist-git repository, so it can look
    at the commit logs.

    For testing purposes the option ``list-bugs`` is provided which calls shared function ``dist_git_bugs`` and prints
    a list of found bugs in the commit log. You can also use the option ``git-repo-path`` to skip cloning and use
    the repository under given path.
    """

    name = 'dist-git'
    description = 'Provide dist-git repository for an artifact.'
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = [
        ('General options', {
            'method': {
                'help': 'What method to use for resolving dist-git repository (default: %(default)s).',
                'choices': ('artifact',),
                'default': 'artifact'
            },
        }),
        ('Testing options', {
            'git-repo-path': {
                'help': 'Use given git repository path. Skips cloning of new repository.',
                'metavar': 'PATH'
            },
            'list-bugs': {
                'help': 'List bugs gathered from dist-git commit logs.',
                'action': 'store_true'
            },
        }),
        ("Options for method 'artifact'", {
            'branch-map': {
                'help': 'Path to a pattern map for mapping artifact target to dist-git branch'
            },
            'clone-url-map': {
                'help': 'Path to a pattern map for mapping artifact type to dist-git repository clone URL'
            },
            'web-url-map': {
                'help': 'Path to a pattern map for mapping artifact type to dist-git repository web URL'
            },
            'branch': {
                'help': 'Force dist-git branch'
            },
            'ref': {
                'help': 'Force dist-git ref'
            },
            'clone-url': {
                'help': 'Force dist-git repository clone URL'
            },
            'clone-args': {
                'help': 'Additional arguments to pass to clone command (default: none)',
                'action': 'append',
                'default': []
            },
            'web-url': {
                'help': 'Force dist-git repository web URL'
            }
        }),
        ("Options related to discovering bugs from commit logs", {
            'regex-bugzilla': {
                'help': 'Regular expression for matching bugzilla in commit logs'
            },
            'regex-resolves': {
                'help': 'Regular expression for matching resolves keyword in commit logs'
            }
        })
    ]

    required_options = ('method',)
    shared_functions = ['dist_git_repository', 'dist_git_bugs']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(DistGit, self).__init__(*args, **kwargs)

        self._repository: Optional[DistGitRepository] = None

        self._regex_resolves: Optional[Pattern[Any]] = None
        self._regex_bugzilla: Optional[Pattern[Any]] = None

    @property
    def eval_context(self) -> Dict[str, DistGitRepository]:
        __content__ = {  # noqa
            'DIST_GIT_REPOSITORY': """
                                    Dist-git repository, represented as ``DistGitRepository`` instance.
                                    """,
        }

        if not self._repository or gluetool_modules_framework.libs.is_recursion(__file__, 'eval_context'):
            return {}

        return {
            'DIST_GIT_REPOSITORY': self._repository,
        }

    @cached_property
    def branch_map(self) -> PatternMap:
        return PatternMap(self.option('branch-map'), logger=self.logger)

    @cached_property
    def clone_url_map(self) -> PatternMap:
        return PatternMap(self.option('clone-url-map'), logger=self.logger)

    @cached_property
    def web_url_map(self) -> PatternMap:
        return PatternMap(self.option('web-url-map'), logger=self.logger)

    def _artifact_branch(self, task: KojiTask) -> Optional[str]:
        # if ref is specified, we cannot use also branch, conflict of both checked in sanity
        if self.option('ref'):
            return None

        elif self.option('branch'):
            return cast(str, self.option('branch'))

        elif self.option('branch-map'):
            assert self.option('branch-map') is not None
            return cast(str, self.branch_map.match(task.target))

        return None

    def _artifact_ref(self, task: KojiTask) -> Optional[str]:
        # if branch is specified, we cannot use also ref, conflict of both checked in sanity
        if self.option('branch'):
            return None

        return self.option('ref') or cast(Optional[str], task.distgit_ref)

    def _artifact_clone_url(self, task: KojiTask) -> str:
        return self.option('clone-url') or cast(str, self.clone_url_map.match(task.ARTIFACT_NAMESPACE))

    def _artifact_web_url(self, task: KojiTask) -> str:
        return self.option('web-url') or cast(str, self.web_url_map.match(task.ARTIFACT_NAMESPACE))

    def _artifact_clone_args(self, task: KojiTask) -> List[str]:
        return normalize_shell_option(self.option('clone-args'))

    _methods_branch = {
        'artifact': _artifact_branch,
    }

    _methods_ref = {
        'artifact': _artifact_ref,
    }

    _methods_clone_url = {
        'artifact': _artifact_clone_url,
    }

    _methods_web_url = {
        'artifact': _artifact_web_url,
    }

    _methods_clone_args = {
        'artifact': _artifact_clone_args,
    }

    def sanity(self) -> None:
        required_options = [
            ('clone-url-map', 'clone-url'),
            ('web-url-map', 'web-url')
        ]

        if not all([self.option(option[0]) or self.option(option[1]) for option in required_options]):
            raise IncompatibleOptionsError("missing required options for method 'artifact'")

        if self.option('ref') and self.option('branch'):
            raise IncompatibleOptionsError("You can use only one of 'ref' or 'branch'")

        regex_resolves = self.option('regex-resolves')
        if regex_resolves:
            try:
                self._regex_resolves = re.compile(regex_resolves, re.IGNORECASE)
            except re.error as error:
                raise gluetool.GlueError("Failed to compile regular expression in 'regex-resolves': {}".format(error))

        regex_bugzilla = self.option('regex-bugzilla')
        if regex_bugzilla:
            try:
                self._regex_bugzilla = re.compile(regex_bugzilla, re.IGNORECASE)
            except re.error as error:
                raise gluetool.GlueError("Failed to compile regular expression in 'regex-bugzilla': {}".format(error))

    def dist_git_repository(self) -> Optional[DistGitRepository]:
        """
        Returns a dist-git repository for the primary_task in the pipeline in the form of an instance
        of the py:class:`DistGitRepository` class. The branch or task can be forced via module parameters
        with the same name.

        The module currently holds only one dist-git repository and it caches it after the first retrieval
        in the execute function.

        :returns: instance of the :py:class:`DistGitRepository`
        """

        return self._repository

    def _acquire_param(self, name: str, error_message: Optional[str] = None) -> Optional[Union[str, List[str]]]:
        """
        For a given repo parameter, pick one of its getter methods and return the value.

        :param str name: name of the repository parameter.
        :param str error_message: if set and the value of parameter is not provided by the getter,
            an exception with this message is raised.
        """

        getter = getattr(self, '_methods_{}'.format(name))[self.option('method')]

        value = getter(self, self.shared('primary_task'))

        if not value:
            if error_message:
                raise gluetool.GlueError(error_message)

            return None

        # Use the initial value as a template for the final value
        context = self.shared('eval_context')

        if isinstance(value, list):
            return [render_template(v, **context) for v in value]
        return render_template(value, **context)

    def dist_git_bugs(self) -> Set[str]:
        """
        Finds and returns bugs referenced in commit logs between primary artifact and a baseline package version.
        See module help for more information about baseline package version.

        :returns set(str): Set of Bugzilla IDs found in commit log.
        """
        artifact = self.shared('primary_task')
        baseline = self.shared('primary_task').baseline_task

        if not baseline:
            raise gluetool.GlueError('No baseline package available')

        if not self._regex_resolves or not self._regex_bugzilla:
            raise gluetool.GlueError("Required options 'regex-resolves' or 'regex-bugzilla' were not set")

        head = artifact.distgit_ref
        tail = baseline.distgit_ref

        assert self._repository is not None
        repository = self._repository

        # clone repository if needed
        if not repository.is_cloned:
            repository.clone(prefix='dist-git-{}-{}'.format(repository.package, repository.branch))

        tail_head = '{}..{}'.format(tail, head)
        log = repository.gitlog('--pretty=%B', tail_head)

        # Extracts bug IDs from dist git log, bugs are unique, thus use a set
        bugs = set()

        # Extracts bug IDs from dist git log, bugs are unique, thus use a set
        for line in log.split('\n'):
            if self._regex_resolves.search(line):
                for bug in self._regex_bugzilla.findall(line):
                    bugs.add(six.ensure_str(bug))

        log_dict(self.info, 'Found bugs in dist-git log', {
            'tail..head': tail_head,
            'bugs': ['BZ#{}'.format(bug) for bug in bugs] if bugs else '<no bugs found>'
        })

        return bugs

    def execute(self) -> None:
        self.require_shared('primary_task')
        task = self.shared('primary_task')
        path = normalize_path(self.option('git-repo-path')) if self.option('git-repo-path') else None

        if not task:
            raise gluetool.GlueError('No task available, cannot continue')

        # Gather repository parameters. Some of them may be missing - ref and branch - because we can
        # use defaults (like `HEAD` and `master`), some are required. Selects correct getter, based on
        # the method.
        kwargs = {
            'clone_url': self._acquire_param('clone_url', error_message='Could not acquire dist-git clone URL'),
            'clone_args': self._acquire_param('clone_args'),
            'web_url': self._acquire_param('web_url', error_message='Could not acquire dist-git web URL'),
            'branch': self._acquire_param('branch'),
            'ref': self._acquire_param('ref'),
            'path': path

        }

        self._repository = DistGitRepository(self.logger, task.component, **kwargs)

        self.info("dist-git repository {}, branch {}, ref {}".format(
            self._repository.web_url,
            self._repository.branch if self._repository.branch else 'not specified',
            self._repository.ref if self._repository.ref else 'not specified'
        ))

        if self.option('list-bugs'):
            self.dist_git_bugs()
