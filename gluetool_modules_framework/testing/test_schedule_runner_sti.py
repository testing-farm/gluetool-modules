# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import tempfile
import os
import re
import stat
import six

from concurrent.futures import ThreadPoolExecutor
import inotify.adapters

import gluetool
from gluetool import GlueError
from gluetool.action import Action
from gluetool.log import log_blob, log_dict
from gluetool.utils import dict_update, normalize_path, normalize_shell_option

from gluetool_modules_framework.libs.artifacts import artifacts_location
from gluetool_modules_framework.libs.test_schedule import (
    TestScheduleResult, TestScheduleEntryOutput, TestScheduleEntryStage
)

# Type annotations
from typing import cast, Any, Callable, Dict, List, Optional, Tuple  # noqa
from gluetool_modules_framework.testing.test_scheduler_sti import TestScheduleEntry  # noqa

from gluetool_modules_framework.libs.results import TestSuite, Log, TestCase


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


def gather_test_results(schedule_entry: TestScheduleEntry, artifacts_directory: str) -> List[TaskRun]:
    """
    Extract detailed test results from 'results.yml' or 'test.log'.
    """

    results: List[TaskRun] = []

    # By default, check results in the new results.yml format
    # https://docs.fedoraproject.org/en-US/ci/standard-test-interface/#_results_format
    results_yml_filename = os.path.join(artifacts_directory, 'results.yml')
    if os.path.isfile(results_yml_filename):
        schedule_entry.debug('Checking results in {}'.format(results_yml_filename))
        try:
            parsed_results = gluetool.utils.load_yaml(results_yml_filename, logger=schedule_entry.logger)
        except gluetool.glue.GlueError:
            schedule_entry.warn('Unable to check results in {}'.format(results_yml_filename))
            return results

        if 'results' in parsed_results and parsed_results['results'] is not None:
            for result in parsed_results['results']:
                results.append(
                    TaskRun(
                        name=result.get('test'),
                        schedule_entry=schedule_entry,
                        result=result.get('result'),
                        logs=result.get('logs', [])))
        else:
            schedule_entry.warn("Results file {} contains nothing under 'results' key".format(results_yml_filename))
        return results

    # Otherwise attempt to parse the old test.log file
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


class STIRunner(gluetool.Module):
    """
    Runs STI-compatible test schedule entries.

    For more information about Standard Test Interface see:

        `<https://fedoraproject.org/wiki/CI/Standard_Test_Interface>`

    Plugin for the "test schedule" workflow.
    """

    name = 'test-schedule-runner-sti'
    description = 'Runs STI-compatible test schedule entries.'
    options = {
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

    shared_functions = ['run_test_schedule_entry', 'serialize_test_schedule_entry_results']

    def _set_schedule_entry_result(self, schedule_entry: TestScheduleEntry) -> None:
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

    def _prepare_environment(self, schedule_entry: TestScheduleEntry) -> Tuple[str, str, str]:
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

    def _run_playbook(self,
                      schedule_entry: TestScheduleEntry,
                      work_dirpath: str,
                      artifact_dirpath: str,
                      inventory_filepath: str) -> List[TaskRun]:
        """
        Run an STI playbook, observe and report results.
        """

        # We're going to spawn new thread for `run_playbook`, therefore we will have to setup its thread
        # root action to the current one of this thread.
        current_action = Action.current_action()

        def _run_playbook_wrapper() -> Any:

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
                ansible_playbook_filepath: Optional[str] = schedule_entry.ansible_playbook_filepath
            elif self.option('ansible-playbook-filepath'):
                ansible_playbook_filepath = normalize_path(self.option('ansible-playbook-filepath'))
            else:
                ansible_playbook_filepath = None

            ansible_environment: Dict[str, str] = {}
            ansible_environment.update(os.environ)
            if schedule_entry.ansible_environment:
                ansible_environment.update(schedule_entry.ansible_environment)

            # `run_playbook` and log the output to the working directory
            self.shared(
                'run_playbook',
                schedule_entry.playbook_filepath,
                schedule_entry.guest,
                inventory=inventory_filepath,
                cwd=artifact_dirpath,
                env=ansible_environment,
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
        run_tests: List[str] = []

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

    def run_test_schedule_entry(self, schedule_entry: TestScheduleEntry) -> None:

        if schedule_entry.runner_capability != 'sti':
            self.overloaded_shared('run_test_schedule_entry', schedule_entry)
            return

        self.require_shared('run_playbook', 'detect_ansible_interpreter')

        self.shared('trigger_event', 'test-schedule-runner-sti.schedule-entry.started',
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

        self.shared('trigger_event', 'test-schedule-runner-sti.schedule-entry.finished',
                    schedule_entry=schedule_entry)

    def serialize_test_schedule_entry_results(self, schedule_entry: TestScheduleEntry, test_suite: TestSuite) -> None:

        if schedule_entry.runner_capability != 'sti':
            self.overloaded_shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)
            return

        if not schedule_entry.results:
            return

        for task in schedule_entry.results:
            test_case = TestCase(name=task.name, result=task.result)

            if task.result.upper() == 'FAIL':
                test_case.failure = True

            if task.result.upper() == 'ERROR':
                test_case.error = True

            # test properties
            assert schedule_entry.guest is not None
            assert schedule_entry.guest.environment is not None
            assert schedule_entry.guest.hostname is not None
            test_case.properties.update({
                'baseosci.arch': str(schedule_entry.guest.environment.arch),
                'baseosci.connectable_host': schedule_entry.guest.hostname,
                'baseosci.distro': str(schedule_entry.guest.environment.compose),
                'baseosci.status': schedule_entry.stage.value.capitalize(),
                'baseosci.variant': '',
            })
            if self.has_shared('dist_git_repository'):
                test_case.properties.update({
                    'baseosci.testcase.source.url': self.shared('dist_git_repository').web_url or ''
                })

            # logs
            assert schedule_entry.artifact_dirpath is not None

            # standard STI logs
            if task.logs:
                for log in task.logs:
                    log_path = os.path.join(schedule_entry.artifact_dirpath, log)

                    schedule_entry.outputs.append(TestScheduleEntryOutput(
                        stage=TestScheduleEntryStage.RUNNING,
                        label=log,
                        log_path=log_path,
                        additional_data=None
                    ))

                    test_case.logs.append(Log(
                        name=log,
                        href=artifacts_location(self, log_path, logger=schedule_entry.logger),
                        schedule_entry=schedule_entry.id,
                        schedule_stage='running'
                    ))

                    # test output can contain invalid utf characters, make sure to replace them
                    if os.path.isfile(log_path):
                        with open(log_path, 'r', errors='replace') as f:
                            test_case.system_out.append(f.read())

                # TODO: remove repeated code in this section
                schedule_entry.outputs.append(TestScheduleEntryOutput(
                    stage=TestScheduleEntryStage.RUNNING,
                    label='log_dir',
                    log_path=schedule_entry.artifact_dirpath,
                    additional_data=None
                ))

                test_case.logs.append(Log(
                    name='log_dir',
                    href=artifacts_location(self, schedule_entry.artifact_dirpath, logger=schedule_entry.logger),
                    schedule_entry=schedule_entry.id,
                    schedule_stage='running'
                ))

            # ansible output only available
            else:
                assert schedule_entry.work_dirpath

                log_path = os.path.join(schedule_entry.work_dirpath, STI_ANSIBLE_LOG_FILENAME)

                schedule_entry.outputs.append(TestScheduleEntryOutput(
                    stage=TestScheduleEntryStage.RUNNING,
                    label=STI_ANSIBLE_LOG_FILENAME,
                    log_path=log_path,
                    additional_data=None
                ))

                test_case.logs.append(Log(
                    name=STI_ANSIBLE_LOG_FILENAME,
                    href=artifacts_location(self, log_path, logger=schedule_entry.logger),
                    schedule_entry=schedule_entry.id,
                    schedule_stage='running'
                ))

            assert schedule_entry.testing_environment is not None
            test_case.requested_environment = schedule_entry.testing_environment
            test_case.provisioned_environment = schedule_entry.guest.environment

            test_suite.test_cases.append(test_case)
