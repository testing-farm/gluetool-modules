# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re
import tempfile
import bs4
import logging
from mock import MagicMock
from six.moves import builtins

import pytest

import gluetool

import gluetool_modules_framework.testing.test_scheduler_sti
import gluetool_modules_framework.testing.test_schedule_runner_sti

from gluetool_modules_framework.testing.test_scheduler_sti import TestScheduleEntry
from gluetool_modules_framework.testing.test_schedule_runner_sti import TaskRun, gather_test_results
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryOutput, TestScheduleEntryStage
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.guest import NetworkedGuest
from gluetool_modules_framework.libs.results import TestSuite, Results

from . import create_module
from . import patch_shared


ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


def read_asset_file(asset_filename: str):
    with open(os.path.join(ASSETS_DIR, 'test_schedule_sti', asset_filename), 'r') as f:
        return f.read()


# TODO: This unit tests file tests two modules: test-scheduler-sti and test-schedule-runner-sti. The reason is that the
# plan for the future is to join these two modules into one, test-schedule-sti.
@pytest.fixture(name='module_scheduler')
def fixture_module_scheduler():
    return create_module(gluetool_modules_framework.testing.test_scheduler_sti.TestSchedulerSTI)[1]


@pytest.fixture(name='module_runner')
def fixture_module_runner():
    return create_module(gluetool_modules_framework.testing.test_schedule_runner_sti.STIRunner)[1]


def clone_mock(logger=None, prefix=None):
    return os.path.abspath(ASSETS_DIR)


# Testing module test-scheduler-sti
def test_create_test_schedule_empty(module_scheduler):
    assert [] == module_scheduler.shared('create_test_schedule', [])


def test_create_test_schedule_playbook(module_scheduler, monkeypatch):
    # Prepare the module
    option_playbook = ['path/to/playbook1', 'another/playbook2']
    option_playbook_variables = ['key1=value1#key 2=value 2']
    testing_environment_constraints = [
        TestingEnvironment(
            arch='x86_64',
            compose='Fedora37',
            pool='some-pool',
            artifacts=[{
                'type': 'koji-build',
                'id': 123456
            }],
            hardware={
                'memory': '>4GiB'
            },
            settings={
                'some-setting': 'some-setting-value'
            }
        )
    ]

    module_scheduler._config.update({
        'playbook': option_playbook,
        'playbook-variables': option_playbook_variables
    })
    patch_shared(monkeypatch, module_scheduler, {}, callables={
        'eval_context': lambda: {}
    })

    # Expected values in the results
    expected_test_schedules = [TestSchedule(), TestSchedule()]
    for expected_test_schedule in expected_test_schedules:
        for playbook in option_playbook:
            entry = TestScheduleEntry(
                gluetool.log.Logging.get_logger(),
                gluetool.utils.normalize_path(playbook),
                {'key1': 'value1', 'key 2': 'value 2'}
            )
            entry.testing_environment = TestingEnvironment(
                arch='x86_64',
                compose='Fedora37',
                pool='some-pool',
                artifacts=[{
                    'type': 'koji-build',
                    'id': 123456
                }],
                hardware={
                    'memory': '>4GiB'
                },
                settings={
                    'some-setting': 'some-setting-value'
                }
            )
            expected_test_schedule.append(entry)

    # Run the module
    test_schedule = module_scheduler.shared('create_test_schedule', testing_environment_constraints)

    # Check the results
    assert len(test_schedule) == 2
    assert test_schedule[0].testing_environment == expected_test_schedule[0].testing_environment
    assert test_schedule[0].variables == expected_test_schedule[0].variables
    assert test_schedule[1].testing_environment == expected_test_schedule[1].testing_environment
    assert test_schedule[1].variables == expected_test_schedule[1].variables


def test_create_test_schedule_repo_request(module_scheduler, monkeypatch):
    patch_shared(monkeypatch, module_scheduler, {}, callables={
        'dist_git_repository': lambda: MagicMock(package='somepackage', branch='somebranch', clone=clone_mock),
        'testing_farm_request': lambda: MagicMock(
            package='somepackage',
            branch='somebranch',
            clone=clone_mock,
            sti=MagicMock(playbooks=['testing_farm/request1.json'])
        )
    })
    testing_environment_constraints = [TestingEnvironment(arch='x86_64', compose='Fedora37')]

    test_schedule = module_scheduler.shared('create_test_schedule', testing_environment_constraints)
    assert len(test_schedule) == 1
    assert test_schedule[0].testing_environment == testing_environment_constraints[0]


def test_create_test_schedule_repo_no_request(module_scheduler, monkeypatch):
    module_scheduler._config.update({
        'sti-tests': 'testing_farm/request1.json'
    })
    patch_shared(monkeypatch, module_scheduler, {}, callables={
        'dist_git_repository': lambda: MagicMock(package='somepackage', branch='somebranch', clone=clone_mock)})
    testing_environment_constraints = [TestingEnvironment(arch='x86_64', compose='Fedora37')]

    test_schedule = module_scheduler.shared('create_test_schedule', testing_environment_constraints)
    assert len(test_schedule) == 1
    assert test_schedule[0].testing_environment == testing_environment_constraints[0]


# Testing module test-schedule-runner-sti
@pytest.mark.parametrize('results_filename, results_content, expected_results', [
    ('test.log', read_asset_file('test.log'), ('result', TestScheduleResult.PASSED, TestScheduleResult.PASSED)),
    ('results.yml', read_asset_file('results.yaml'), ('foo', TestScheduleResult.FAILED, TestScheduleResult.FAILED)),
])
def test_run_test_schedule_entry(module_runner, monkeypatch, results_filename, results_content, expected_results):
    with tempfile.TemporaryDirectory(prefix='test-schedule-runner-sti') as tmpdir:
        # Prepare the module
        def run_playbook_mock(playbook_filepath, guest, inventory, cwd=None, json_output=False, log_filepath=None,
                              variables=None, ansible_playbook_filepath=None, extra_options=None, env=None):
            assert env["_TESTSE1"] == "_TESTVAL11"  # overridden
            assert env["_TESTSE2"] == "_TESTVAL2"  # kept from os.environ
            assert env["_TESTSE3"] == "_TESTVAL3"  # passed in ansible_environment
            with open(os.path.join(cwd, results_filename), 'w') as file:
                file.write(results_content)

        module_runner._config.update({
            'watch-timeout': 1
        })
        schedule_entry = TestScheduleEntry(
            gluetool.log.Logging().get_logger(),
            gluetool.utils.normalize_path(os.path.join(tmpdir, 'playbook1.yaml')),
            {}
        )
        schedule_entry.guest = NetworkedGuest(module_runner, 'hostname', 'name')

        patch_shared(monkeypatch, module_runner, {}, callables={
            'run_playbook': run_playbook_mock,
            'detect_ansible_interpreter': lambda _: None,
        })

        schedule_entry.runner_capability = 'sti'

        # Expected values in the output
        expected_task_name, expected_task_result, expected_schedule_entry_result = expected_results

        # Test ansible_environment functionality
        monkeypatch.setenv("_TESTSE1", "_TESTVAL1")
        monkeypatch.setenv("_TESTSE2", "_TESTVAL2")
        schedule_entry.ansible_environment = {"_TESTSE1": "_TESTVAL11", "_TESTSE3": "_TESTVAL3"}
        # Run the module - it creates new directories from the current working directory, temporarily change it to
        # the tmpdir so it gets cleaned up later
        with monkeypatch.context() as m:
            m.chdir(tmpdir)
            module_runner.shared('run_test_schedule_entry', schedule_entry)

        # Check the results
        assert re.match(r'^work-playbook1.yaml[a-z0-9_]+$', schedule_entry.work_dirpath)
        assert re.match(r'^work-playbook1.yaml[a-z0-9_]+/tests-[a-z0-9_]+$', schedule_entry.artifact_dirpath)

        assert re.match(
            r'^' + re.escape(tmpdir) + r'/?work-playbook1.yaml[a-z0-9_]+/inventory-[a-z0-9_]+$',
            schedule_entry.inventory_filepath
        )

        task_run = schedule_entry.results[0]
        assert task_run.name == expected_task_name
        assert task_run.result == expected_task_result
        assert schedule_entry.result == expected_schedule_entry_result


@pytest.mark.parametrize('schedule_entry_results, expected_schedule_entry_outputs, expected_xml, expected_junit', [
    ([], [], None, None),
    (
        [TaskRun(name='foo', schedule_entry=None, result=TestScheduleResult.FAILED, logs=None)],
        [
            TestScheduleEntryOutput(
                stage=TestScheduleEntryStage.RUNNING,
                label='ansible-output.txt',
                log_path='some/work-dirpath/ansible-output.txt',
                additional_data=None
            )
        ],
        read_asset_file('results1.xml'),
        read_asset_file('results-junit1.xml')
    ),
    (
        [TaskRun(name='foo', schedule_entry=None, result=TestScheduleResult.ERROR, logs=['log1', 'log2'])],
        [
            TestScheduleEntryOutput(
                stage=TestScheduleEntryStage.RUNNING,
                label='log1',
                log_path='some/artifact-dirpath/log1',
                additional_data=None
            ),
            TestScheduleEntryOutput(
                stage=TestScheduleEntryStage.RUNNING,
                label='log2',
                log_path='some/artifact-dirpath/log2',
                additional_data=None
            ),
            TestScheduleEntryOutput(
                stage=TestScheduleEntryStage.RUNNING,
                label='log_dir',
                log_path='some/artifact-dirpath',
                additional_data=None
            )
        ],
        read_asset_file('results2.xml'),
        read_asset_file('results-junit2.xml')
    )
])
def test_serialize_test_schedule_entry_results(module_runner, schedule_entry_results,
                                               expected_schedule_entry_outputs, expected_xml, expected_junit):
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        gluetool.utils.normalize_path('another/playbook2'),
        {}
    )
    schedule_entry.artifact_dirpath = 'some/artifact-dirpath'
    schedule_entry.work_dirpath = 'some/work-dirpath'
    schedule_entry.guest = NetworkedGuest(module_runner, 'hostname', 'name')
    schedule_entry.guest.environment = TestingEnvironment(arch='x86_64', compose='rhel-9')
    schedule_entry.testing_environment = TestingEnvironment(arch='x86_64', compose='rhel-9')
    schedule_entry.results = schedule_entry_results
    schedule_entry.runner_capability = 'sti'
    test_suite = TestSuite(name='some-suite', result=TestScheduleResult.PASSED)
    results = Results(test_suites=[test_suite], test_schedule_result=TestScheduleResult.PASSED,
                      overall_result=TestScheduleResult.PASSED)

    module_runner.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    assert schedule_entry.outputs == expected_schedule_entry_outputs

    if expected_xml:
        assert results.xunit_testing_farm.to_xml_string(pretty_print=True) == expected_xml
        assert results.xunit.to_xml_string(pretty_print=True) == expected_junit


def test_serialize_to_junit_non_printable_characters(monkeypatch, module_runner):
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        gluetool.utils.normalize_path('another/playbook2'),
        {}
    )
    schedule_entry.artifact_dirpath = 'some/artifact-dirpath'
    schedule_entry.work_dirpath = 'some/work-dirpath'
    schedule_entry.guest = NetworkedGuest(module_runner, 'hostname', 'name')
    schedule_entry.guest.environment = TestingEnvironment(arch='x86_64', compose='rhel-9')
    schedule_entry.testing_environment = TestingEnvironment(arch='x86_64', compose='rhel-9')
    schedule_entry.results = [TaskRun(name='foo', schedule_entry=None, result=TestScheduleResult.PASSED, logs=['log1'])]
    schedule_entry.runner_capability = 'sti'
    test_suite = TestSuite(name='some-suite', result=TestScheduleResult.PASSED)
    results = Results(test_suites=[test_suite], test_schedule_result='some-schedule-result',
                      overall_result='some-overall-result')

    expected_junit = read_asset_file('results-junit3.xml')

    # mock for
    # with open('filename', 'r') as f:
    #     f.read()
    class FileMock(MagicMock):
        def read(self, *args, **kwargs):
            # put some non-printable character, they should not be in the xml file
            # new line character should be intact
            return 'output-line1\noutput-line2\x08\x03\t\x1f'

    class OpenMock(MagicMock):
        def __enter__(self):
            return FileMock()

        def __exit__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(builtins, 'open', OpenMock)
    monkeypatch.setattr(os.path, 'isfile', MagicMock(return_value=True))

    module_runner.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    assert results.xunit.to_xml_string(pretty_print=True) == expected_junit


@pytest.mark.parametrize('workdir, expected_message, expected_results', [
    (
        os.path.join(ASSETS_DIR, 'test_schedule_sti', 'workdir-results-empty'),
        "Results file gluetool_modules_framework/tests/assets/test_schedule_sti/workdir-results-empty/results.yml contains nothing under 'results' key",
        []
    ),
    (
        'some/non/existent/path',
        'Unable to check results in some/non/existent/path/test.log',
        []
    )
])
def test_gather_results_empty(log, workdir, expected_message, expected_results):
    results = gather_test_results(
        gluetool.log.Logging.get_logger(),
        workdir
    )
    assert log.match(levelno=logging.WARN, message=expected_message)
    assert results == expected_results
