# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import glob
import os.path

import gluetool
from gluetool import GlueError, utils

import gluetool_modules_framework
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleEntry as BaseTestScheduleEntry
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest

# Type annotations
from typing import Any, cast, Dict, List, Optional  # noqa


class TestScheduleEntry(BaseTestScheduleEntry):
    def __init__(self, logger: gluetool.log.ContextAdapter, playbook_filepath: str, variables: Dict[str, Any]) -> None:
        """
        Test schedule entry, suited for use with STI runners.

        :param logger: logger used as a parent of this entry's own logger.
        :param str playbook_filepath: path to a STI-compatible playbook.
        """

        # Let the ID be playbook's subpath with regard to the current directory - it's much shorter,
        # it doesn't make much sense to print its parents like Jenkins' workdir and so on.
        se_id = os.path.relpath(playbook_filepath)

        super(TestScheduleEntry, self).__init__(
            logger,
            se_id,
            'sti'
        )

        self.playbook_filepath = playbook_filepath
        self.variables = variables
        self.work_dirpath: Optional[str] = None
        self.artifact_dirpath: Optional[str] = None
        self.inventory_filepath: Optional[str] = None
        self.results: Any = None
        self.ansible_playbook_filepath: Optional[str] = None

    def log_entry(self, log_fn: Optional[gluetool.log.LoggingFunctionType] = None) -> None:

        log_fn = log_fn or self.debug

        super(TestScheduleEntry, self).log_entry(log_fn=log_fn)

        log_fn('playbook path: {}'.format(self.playbook_filepath))


class TestSchedulerSTI(gluetool.Module):
    """
    Creates test schedule entries for ``test-scheduler`` module by inspecting STI configuration.

    By default, attempts to find all Ansible playbooks as defined by Standard Test Interface format,
    in the dist-git repository of the artifact. For access to the repository, ``dist_git_repository``
    shared function is used.

    The module can also execute a specific testing playbook(s), skipping the retrieval from dist-git.
    See the ``--playbook`` option for more information.

    For more information about Standard Test Interface see:

        `<https://fedoraproject.org/wiki/CI/Standard_Test_Interface>`

    Plugin for the "test schedule" workflow.
    """

    name = 'test-scheduler-sti'
    description = 'Create test schedule entries for ``test-scheduler`` module by inspecting STI configuration.'
    options = {
        'playbook': {
            'help': 'Use the given ansible playbook(s) for execution, skip dist-git retrieval.',
            'metavar': 'PLAYBOOK',
            'action': 'append'
        },
        'playbook-variables': {
            'help': 'List of hash-separated pairs <variable name>=<variable value> (default: none).',
            'metavar': 'KEY=VALUE',
            'action': 'append',
            'default': []
        },
        'sti-tests': {
            'help': """
                    Use the given glob when searching for STI tests in the dist-git
                    repository clone (default: %(default)s).
                    """,
            'metavar': 'GLOB',
            'default': 'tests/tests*.yml'
        }
    }

    shared_functions = ['create_test_schedule']

    def _playbooks_from_dist_git(self, repodir: str, tests: Optional[str] = None) -> List[str]:
        """
        Return STI playbooks (tests) from dist-git.

        :param str repodir: clone of a dist-git repository.
        :param str tests: tests to override the module option 'sti-tests'.
        """

        playbooks = glob.glob('{}/{}'.format(repodir, tests or self.option('sti-tests')))

        if not playbooks:
            raise gluetool_modules_framework.libs.test_schedule.EmptyTestScheduleError(
                self.shared('primary_task') or self.shared('testing_farm_request')
            )

        return playbooks

    def create_test_schedule(
        self,
        testing_environment_constraints: Optional[List[TestingEnvironment]] = None
    ) -> TestSchedule:
        """
        Create a test schedule based on either content of artifact's dist-git repository,
        or using playbooks specified via ``--playbook`` option.

        :param list(gluetool_modules_framework.libs.testing_environment.TestingEnvironment)
            testing_environment_constraints:
                limitations put on us by the caller. In the form of testing environments - with some fields possibly
                left unspecified - the list specifies what environments are expected to be used for testing.
                At this moment, only ``arch`` property is obeyed.
        :returns: a test schedule consisting of :py:class:`TestScheduleEntry` instances.
        """

        playbooks = []

        if not testing_environment_constraints:
            self.warn('STI scheduler does not support open constraints', sentry=True)
            return TestSchedule()

        # get playbooks (tests) from command-line or dist-git
        if self.option('playbook'):
            playbooks = gluetool.utils.normalize_path_option(self.option('playbook'))

        else:
            try:
                self.require_shared('dist_git_repository')

                repository = self.shared('dist_git_repository')

            except GlueError as exc:
                raise GlueError('Could not locate dist-git repository: {}'.format(exc))

            try:
                prefix = repository.clonedir_prefix

                # If prefix has / it leads to "No such directory" error
                prefix = prefix.replace('/', '-')

                repodir = repository.clone(
                    logger=self.logger,
                    prefix=prefix
                )

            except GlueError:
                raise GlueError('Could not clone {} branch of {} repository'.format(
                    repository.branch, repository.clone_url))

            request: Optional[TestingFarmRequest] = self.shared('testing_farm_request')
            if request and request.sti and request.sti.playbooks:
                for tests in request.sti.playbooks:
                    playbooks.extend(self._playbooks_from_dist_git(repodir, tests))

            else:
                playbooks = self._playbooks_from_dist_git(repodir)

        gluetool.log.log_dict(self.info, 'creating schedule for {} playbooks'.format(len(playbooks)), playbooks)

        # Playbook variables are separated by hash. We cannot use comma, because value of the variable
        # can be list. Also we cannot use space, because space separates module options.
        playbook_variables = utils.normalize_multistring_option(self.option('playbook-variables'), separator='#')

        variables = {}
        context = self.shared('eval_context')

        for variable in playbook_variables:
            if not variable or '=' not in variable:
                raise gluetool.GlueError("'{}' is not correct format of variable".format(variable))

            # `maxsplit=1` is optional parameter in Python2 and keyword parameter in Python3
            # using as optional to work properly in both
            key, value = variable.split('=', 1)

            variables[key] = gluetool.utils.render_template(value, logger=self.logger, **context)

        schedule = TestSchedule()

        # For each playbook, architecture and compose, create a schedule entry
        for playbook in playbooks:
            for tec in testing_environment_constraints:
                if tec.arch == tec.ANY:
                    self.warn('STI scheduler does not support open constraints', sentry=True)
                    continue

                schedule_entry = TestScheduleEntry(gluetool.log.Logging.get_logger(), playbook, variables)

                schedule_entry.testing_environment = TestingEnvironment(
                    compose=tec.compose,
                    arch=tec.arch,
                    snapshots=tec.snapshots
                )

                schedule.append(schedule_entry)

        schedule.log(self.debug, label='complete schedule')

        return schedule
