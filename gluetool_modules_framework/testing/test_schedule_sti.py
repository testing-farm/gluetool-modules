# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import glob
import collections
import tempfile
import os
import os.path
import re
import stat
import six

from concurrent.futures import ThreadPoolExecutor
import inotify.adapters

import gluetool
from gluetool import GlueError, utils
from gluetool.action import Action
from gluetool.log import log_blob, log_dict
from gluetool.utils import dict_update, new_xml_element, normalize_path, normalize_shell_option

import gluetool_modules_framework
from gluetool_modules_framework.libs import sort_children
from gluetool_modules_framework.libs.artifacts import artifacts_location
from gluetool_modules_framework.testing_farm.testing_farm_request import TestingFarmRequest
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import (
    TestScheduleResult, TestScheduleEntryOutput, TestScheduleEntryStage, TestSchedule,
    TestScheduleEntry as BaseTestScheduleEntry
)

# Type annotations
from typing import cast, Any, Callable, Dict, List, Optional, Tuple  # noqa


# Check whether Ansible finished running tests every 5 seconds.
DEFAULT_WATCH_TIMEOUT = 5

STI_ANSIBLE_LOG_FILENAME = 'ansible-output.txt'

#: Represents a single run of a test - one STI playbook can contain multiple such tests
#  - and results of this run.
#:
#: :ivar str name: name of the test.
#: :ivar libs.test_schedule.TestScheduleEntry schedule_entry: test schedule entry the task belongs to.
#: :ivar dict results: results of the test run, as reported by Ansible playbook log.
#: :ivar dict logs: list of logs associated with the test
TaskRun = collections.namedtuple('TaskRun', ('name', 'schedule_entry', 'result', 'logs'))


def gather_test_results(schedule_entry, artifacts_directory):
    # type: (TestScheduleEntry, str) -> List[TaskRun]
    """
    Extract detailed test results from 'results.yml' or 'test.log'.
    """

    results = []

    # By default, check results in the new results.yml format
    # https://docs.fedoraproject.org/en-US/ci/standard-test-interface/#_results_format
    results_yml_filename = os.path.join(artifacts_directory, 'results.yml')
    if os.path.isfile(results_yml_filename):
        schedule_entry.debug('Checking results in {}'.format(results_yml_filename))
        try:
            parsed_results = gluetool.utils.load_yaml(results_yml_filename, logger=schedule_entry.logger)
            for result in parsed_results['results']:
                results.append(
                    TaskRun(
                        name=result.get('test'),
                        schedule_entry=schedule_entry,
                        result=result.get('result'),
                        logs=result.get('logs', [])))
        except gluetool.glue.GlueError:
            schedule_entry.warn('Unable to check results in {}'.format(results_yml_filename))

    # Otherwise attempt to parse the old test.log file
    else:
        test_log_filename = os.path.join(artifacts_directory, 'test.log')
        schedule_entry.debug('Checking results in {}'.format(test_log_filename))
        try:
            with open(test_log_filename) as test_log:
                for line in test_log:
                    match = re.match('([^ :]+):? (.*)', line)
                    if not match:
                        continue
                    result, name = match.groups()
                    results.append(TaskRun(
                        name=name, schedule_entry=schedule_entry, result=result, logs=[]))
        except IOError:
            schedule_entry.warn('Unable to check results in {}'.format(test_log_filename))

    return results


class TestScheduleEntry(BaseTestScheduleEntry):
    def __init__(self, logger, playbook_filepath, variables):
        # type: (gluetool.log.ContextAdapter, str, Dict[str, Any]) -> None
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
        self.work_dirpath = None  # type: Optional[str]
        self.artifact_dirpath = None  # type: Optional[str]
        self.inventory_filepath = None  # type: Optional[str]
        self.results = None  # type: Any
        self.ansible_playbook_filepath = None  # type: Optional[str]

    def log_entry(self, log_fn=None):
        # type: (Optional[gluetool.log.LoggingFunctionType]) -> None

        log_fn = log_fn or self.debug

        super(TestScheduleEntry, self).log_entry(log_fn=log_fn)

        log_fn('playbook path: {}'.format(self.playbook_filepath))


class TestScheduleSTI(gluetool.Module):
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

    name = 'test-schedule-sti'
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
        },
        'watch-timeout': {
            'help': 'Check whether Ansible finished running tests every SECONDS seconds. (default: %(default)s)',
            'metavar': 'SECONDS',
            'type': int,
            'default': DEFAULT_WATCH_TIMEOUT
        },
        'ansible-playbook-filepath': {
            'help': """
                    Provide different ansible-playbook executable to the call
                    of a `run_playbook` shared function. (default: %(default)s)
                    """,
            'metavar': 'PATH',
            'type': str,
            'default': ''
        },
        'ansible-extra-options': {
            'help': 'Extra options to pass to ansible-playbook',
        }
    }

    shared_functions = ['create_test_schedule', 'run_test_schedule_entry', 'serialize_test_schedule_entry_results']

    def _playbooks_from_dist_git(self, repodir, tests=None):
        # type: (str, Optional[str]) -> List[str]
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

    def create_test_schedule(self, testing_environment_constraints=None):
        # type: (Optional[List[TestingEnvironment]]) -> TestSchedule
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
                prefix = 'dist-git-{}-{}-'.format(repository.package, repository.branch)
                # If prefix has / it leads to "No such directory" error
                prefix = prefix.replace('/', '-')

                repodir = repository.clone(
                    logger=self.logger,
                    prefix=prefix
                )

            except GlueError:
                raise GlueError('Could not clone {} branch of {} repository'.format(
                    repository.branch, repository.clone_url))

            request = self.shared('testing_farm_request')  # type: Optional[TestingFarmRequest]
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

    def _set_schedule_entry_result(self, schedule_entry):
        # type: (TestScheduleEntry) -> None
        """
        Try to find at least one task that didn't complete or didn't pass.
        """

        self.debug('Try to find any non-PASS task')

        for task_run in schedule_entry.results:
            schedule_entry, task, result = task_run.schedule_entry, task_run.name, task_run.result

            schedule_entry.debug('  {}: {}'.format(task, result))

            if result.lower() == 'pass':
                continue

            schedule_entry.debug('    We have our traitor!')
            schedule_entry.result = TestScheduleResult.FAILED
            return

        schedule_entry.result = TestScheduleResult.PASSED

    def _prepare_environment(self, schedule_entry):
        # type: (TestScheduleEntry) -> Tuple[str, str, str]
        """
        Prepare local environment for running the schedule entry, by setting up some directories and files.

        :returns: a path to a work directory, dedicated for this entry, and path to a "artifact" directory
            in which entry's artifacts are supposed to appear.
        """

        assert schedule_entry.guest is not None

        # Create a working directory, we try hard to keep all the related work inside this directory.
        # Under this directory, there will be an inventory file and an "artifact" directory in which
        # the Ansible is supposed to run - all artifacts created by the playbook will therefore land
        # in the artifact directory.

        work_dir_prefix = 'work-{}'.format(os.path.basename(schedule_entry.playbook_filepath))
        artifact_dir_prefix = 'tests-'

        # tempfile.mkdtemp returns an absolute path to the directory, but the unspoken convention says
        # we must use paths that are relative to the current working directory. Therefore we must make
        # both schedule entry's work dir and artifact dir relative to the CWD.
        work_dir = os.path.relpath(
            tempfile.mkdtemp(dir=os.getcwd(), prefix=work_dir_prefix),
            os.getcwd()
        )

        artifact_dir = os.path.relpath(
            tempfile.mkdtemp(dir=work_dir, prefix=artifact_dir_prefix),
            os.getcwd()
        )

        # Make sure it's possible to enter our directories for other parties. We're not that concerned with privacy,
        # we'd rather let common users inside the directories when inspecting the pipeline artifacts. Therefore
        # setting their permissions to ug=rwx,o=rx.

        os.chmod(
            work_dir,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH  # noqa: E501  # line too long
        )

        os.chmod(
            artifact_dir,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH  # noqa: E501  # line too long
        )

        schedule_entry.info("working directory '{}'".format(work_dir))

        # try to detect ansible interpreter
        interpreters = self.shared('detect_ansible_interpreter', schedule_entry.guest)

        # inventory file contents
        ansible_interpreter = 'ansible_python_interpreter={}'.format(interpreters[0]) if interpreters else ''
        inventory_content = """
[localhost]
sut     ansible_host={} ansible_user=root {}
""".format(schedule_entry.guest.hostname, ansible_interpreter)

        with tempfile.NamedTemporaryFile(delete=False, dir=work_dir, prefix='inventory-') as inventory:
            log_blob(schedule_entry.info, 'using inventory', inventory_content)

            inventory.write(six.ensure_binary(inventory_content))
            inventory.flush()

        # Inventory file's permissions are limited to user only, u=rw,go=. That's far from being perfect, hard
        # to examine such file, hence one more chmod to u=rw,go=r

        os.chmod(
            inventory.name,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
        )

        return work_dir, artifact_dir, inventory.name

    def _run_playbook(self, schedule_entry, work_dirpath, artifact_dirpath, inventory_filepath):
        # type: (TestScheduleEntry, str, str, str) -> List[TaskRun]
        """
        Run an STI playbook, observe and report results.
        """

        # We're going to spawn new thread for `run_playbook`, therefore we will have to setup its thread
        # root action to the current one of this thread.
        current_action = Action.current_action()

        def _run_playbook_wrapper():
            # type: () -> Any

            assert schedule_entry.guest is not None

            Action.set_thread_root(current_action)

            context = dict_update(
                self.shared('eval_context'),
                {
                    'GUEST': schedule_entry.guest
                }
            )

            variables = dict_update(
                {},
                {
                    # Internally we're working with CWD-relative path but we have to feed ansible
                    # with the absolute one because it operates from its own, different cwd.
                    'artifacts': os.path.abspath(artifact_dirpath),
                    'ansible_ssh_common_args': ' '.join(['-o ' + option for option in schedule_entry.guest.options])
                },
                self.shared('user_variables', logger=schedule_entry.logger, context=context) or {},
                schedule_entry.variables
            )

            if schedule_entry.ansible_playbook_filepath:
                ansible_playbook_filepath = schedule_entry.ansible_playbook_filepath  # type: Optional[str]
            elif self.option('ansible-playbook-filepath'):
                ansible_playbook_filepath = normalize_path(self.option('ansible-playbook-filepath'))
            else:
                ansible_playbook_filepath = None

            # `run_playbook` and log the output to the working directory
            self.shared(
                'run_playbook',
                schedule_entry.playbook_filepath,
                schedule_entry.guest,
                inventory=inventory_filepath,
                cwd=artifact_dirpath,
                json_output=False,
                log_filepath=os.path.join(work_dirpath, STI_ANSIBLE_LOG_FILENAME),
                variables=variables,
                ansible_playbook_filepath=ansible_playbook_filepath,
                extra_options=normalize_shell_option(self.option('ansible-extra-options'))
            )

        # monitor artifact directory
        notify = inotify.adapters.Inotify()
        notify.add_watch(artifact_dirpath)

        # initial values
        run_tests = []  # type: List[str]

        # testname matching regex
        testname_regex = re.compile(r'^\.?([^_]*)_(.*).log.*$')

        # run the playbook in a separate thread
        with ThreadPoolExecutor(thread_name_prefix='testing-thread') as executor:
            future = executor.submit(_run_playbook_wrapper)

            # monitor the test execution
            while True:
                for event in notify.event_gen(yield_nones=False, timeout_s=self.option('watch-timeout')):
                    (_, event_types, path, filename) = event

                    self.debug("PATH=[{}] FILENAME=[{}] EVENT_TYPES={}".format(path, filename, event_types))

                    # we lookup testing progress by looking at their logs being created
                    if 'IN_CREATE' not in event_types:
                        continue

                    # try to match the test log name
                    match = re.match(testname_regex, filename)

                    if not match:
                        continue

                    result, testname = match.groups()

                    # do not log the test multiple times
                    if testname not in run_tests:
                        run_tests.append(testname)
                        schedule_entry.info("{} - {}".format(testname, result))

                # handle end of execution
                if future.done():
                    break

        # parse results
        results = gather_test_results(schedule_entry, artifact_dirpath)

        try:
            future.result()

        except GlueError:

            # STI defines that Ansible MUST fail if any of the tests fail. To differentiate from a generic ansible
            # error, we check if required test.log was generated with at least one result.
            # Note that Ansible error is still a user error though, nothing we can do anything about, in case ansible
            # failed, report the ansible output as the test result.
            if not results:
                results.append(TaskRun(name='ansible', schedule_entry=schedule_entry, result='FAIL', logs=[]))

        return results

    def run_test_schedule_entry(self, schedule_entry):
        # type: (TestScheduleEntry) -> None

        if schedule_entry.runner_capability != 'sti':
            self.overloaded_shared('run_test_schedule_entry', schedule_entry)
            return

        self.require_shared('run_playbook', 'detect_ansible_interpreter')

        self.shared('trigger_event', 'test-schedule-sti.schedule-entry.started',
                    schedule_entry=schedule_entry)

        # We don't need the working directory actually - we need artifact directory, which is
        # a subdirectory of working directory. But one day, who knows...
        work_dirpath, artifact_dirpath, inventory_filepath = self._prepare_environment(schedule_entry)
        schedule_entry.work_dirpath = work_dirpath
        schedule_entry.artifact_dirpath = artifact_dirpath
        schedule_entry.inventory_filepath = inventory_filepath

        ansible_log_filepath = os.path.join(work_dirpath, STI_ANSIBLE_LOG_FILENAME)

        artifacts = artifacts_location(self, ansible_log_filepath, logger=schedule_entry.logger)

        schedule_entry.info('Ansible logs are in {}'.format(artifacts))

        results = self._run_playbook(schedule_entry, work_dirpath, artifact_dirpath, inventory_filepath)

        schedule_entry.results = results

        log_dict(schedule_entry.debug, 'results', results)

        self._set_schedule_entry_result(schedule_entry)

        self.shared('trigger_event', 'test-schedule-sti.schedule-entry.finished',
                    schedule_entry=schedule_entry)

    def serialize_test_schedule_entry_results(self, schedule_entry, test_suite):
        # type: (TestScheduleEntry, Any) -> None

        def _add_property(properties, name, value):
            # type: (Any, str, str) -> Any
            return new_xml_element('property', _parent=properties, name='baseosci.{}'.format(name), value=value or '')

        def _add_log(logs, name, path, href, schedule_entry=None):
            # type: (Any, str, str, str, Optional[TestScheduleEntry]) -> Any

            attrs = {
                'name': name,
                'href': href,
                'schedule-stage': 'running'
            }

            if schedule_entry is not None:
                attrs['schedule-entry'] = schedule_entry.id

                schedule_entry.outputs.append(TestScheduleEntryOutput(
                    stage=TestScheduleEntryStage.RUNNING,
                    label=name,
                    log_path=path,
                    additional_data=None
                ))

            return new_xml_element(
                'log',
                _parent=logs,
                **attrs
            )

        def _add_testing_environment(test_case, name, arch, compose):
            # type: (Any, str, Any, Any) -> Any
            parent_elem = new_xml_element('testing-environment', _parent=test_case, name=name)
            new_xml_element('property', _parent=parent_elem, name='arch', value=arch)
            new_xml_element('property', _parent=parent_elem, name='compose', value=compose)

        if schedule_entry.runner_capability != 'sti':
            self.overloaded_shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)
            return

        if not schedule_entry.results:
            return

        for task in schedule_entry.results:

            test_case = new_xml_element('testcase', _parent=test_suite, name=task.name, result=task.result)
            properties = new_xml_element('properties', _parent=test_case)
            logs = new_xml_element('logs', _parent=test_case)

            if task.result.upper() == 'FAIL':
                new_xml_element('failure', _parent=test_case)

            if task.result.upper() == 'ERROR':
                new_xml_element('error', _parent=test_case)

            # test properties
            assert schedule_entry.guest is not None
            assert schedule_entry.guest.environment is not None
            assert schedule_entry.guest.hostname is not None
            _add_property(properties, 'arch', str(schedule_entry.guest.environment.arch))
            _add_property(properties, 'connectable_host', schedule_entry.guest.hostname)
            _add_property(properties, 'distro', str(schedule_entry.guest.environment.compose))
            _add_property(properties, 'status', schedule_entry.stage.value.capitalize())
            if self.has_shared('dist_git_repository'):
                _add_property(properties, 'testcase.source.url', self.shared('dist_git_repository').web_url)
            _add_property(properties, 'variant', '')

            # logs
            assert schedule_entry.artifact_dirpath is not None

            # standard STI logs
            if task.logs:
                for log in task.logs:
                    log_path = os.path.join(schedule_entry.artifact_dirpath, log)

                    _add_log(
                        logs,
                        name=log,
                        path=log_path,
                        href=artifacts_location(self, log_path, logger=schedule_entry.logger),
                        schedule_entry=schedule_entry
                    )

                _add_log(
                    logs,
                    name="log_dir",
                    path=schedule_entry.artifact_dirpath,
                    href=artifacts_location(self, schedule_entry.artifact_dirpath, logger=schedule_entry.logger),
                    schedule_entry=schedule_entry
                )

            # ansible output only available
            else:
                assert schedule_entry.work_dirpath
                log_path = os.path.join(schedule_entry.work_dirpath, STI_ANSIBLE_LOG_FILENAME)

                _add_log(
                    logs,
                    name=STI_ANSIBLE_LOG_FILENAME,
                    path=log_path,
                    href=artifacts_location(self, log_path, logger=schedule_entry.logger),
                    schedule_entry=schedule_entry
                )

            assert schedule_entry.testing_environment is not None
            _add_testing_environment(test_case, 'requested', schedule_entry.testing_environment.arch,
                                     schedule_entry.testing_environment.compose)
            _add_testing_environment(test_case, 'provisioned', schedule_entry.guest.environment.arch,
                                     schedule_entry.guest.environment.compose)

            # sorting
            sort_children(properties, lambda child: child.attrs['name'])
            sort_children(logs, lambda child: child.attrs['name'])

        test_suite['tests'] = len(schedule_entry.results)
