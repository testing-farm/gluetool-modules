# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re
import tempfile
import bs4
from mock import MagicMock

import pytest

import gluetool

import gluetool_modules_framework.testing.test_schedule_sti

from gluetool_modules_framework.testing.test_schedule_sti import TestScheduleEntry, TaskRun
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryOutput, TestScheduleEntryStage
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.guest import NetworkedGuest

from . import create_module
from . import patch_shared


ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


def read_asset_file(asset_filename: str):
    with open(os.path.join(ASSETS_DIR, 'test_schedule_sti', asset_filename), 'r') as f:
        return f.read()


@pytest.fixture(name='module')
def fixture_module_scheduler():
    return create_module(gluetool_modules_framework.testing.test_schedule_sti.TestScheduleSTI)[1]


def clone_mock(logger=None, prefix=None):
    return os.path.abspath(ASSETS_DIR)


def test_create_test_schedule_empty(module):
    assert [] == module.shared('create_test_schedule', [])


def test_create_test_schedule_playbook(module, monkeypatch):
    # Prepare the module
    option_playbook = ['path/to/playbook1', 'another/playbook2']
    option_playbook_variables = ['key1=value1#key 2=value 2']
    testing_environment_constraints = [TestingEnvironment(arch='x86_64', compose='Fedora37')]
    module._config.update({
        'playbook': option_playbook,
        'playbook-variables': option_playbook_variables
    })
    patch_shared(monkeypatch, module, {}, callables={
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
            entry.testing_environment = TestingEnvironment(arch='x86_64', compose='Fedora37')
            expected_test_schedule.append(entry)

    # Run the module
    test_schedule = module.shared('create_test_schedule', testing_environment_constraints)

    # Check the results
    assert len(test_schedule) == 2
    assert test_schedule[0].testing_environment == expected_test_schedule[0].testing_environment
    assert test_schedule[0].variables == expected_test_schedule[0].variables
    assert test_schedule[1].testing_environment == expected_test_schedule[1].testing_environment
    assert test_schedule[1].variables == expected_test_schedule[1].variables


def test_create_test_schedule_repo_request(module, monkeypatch):
    patch_shared(monkeypatch, module, {}, callables={
        'dist_git_repository': lambda: MagicMock(package='somepackage', branch='somebranch', clone=clone_mock),
        'testing_farm_request': lambda: MagicMock(
            package='somepackage',
            branch='somebranch',
            clone=clone_mock,
            sti=MagicMock(playbooks=['testing_farm/request1.json'])
        )
    })
    testing_environment_constraints = [TestingEnvironment(arch='x86_64', compose='Fedora37')]

    test_schedule = module.shared('create_test_schedule', testing_environment_constraints)
    assert len(test_schedule) == 1
    assert test_schedule[0].testing_environment == testing_environment_constraints[0]


def test_create_test_schedule_repo_no_request(module, monkeypatch):
    module._config.update({
        'sti-tests': 'testing_farm/request1.json'
    })
    patch_shared(monkeypatch, module, {}, callables={
        'dist_git_repository': lambda: MagicMock(package='somepackage', branch='somebranch', clone=clone_mock)})
    testing_environment_constraints = [TestingEnvironment(arch='x86_64', compose='Fedora37')]

    test_schedule = module.shared('create_test_schedule', testing_environment_constraints)
    assert len(test_schedule) == 1
    assert test_schedule[0].testing_environment == testing_environment_constraints[0]


@pytest.mark.parametrize('results_filename, results_content, expected_results', [
    ('test.log', read_asset_file('test.log'), ('result', 'pass', TestScheduleResult.PASSED)),
    ('results.yml', read_asset_file('results.yaml'), ('foo', 'bar', TestScheduleResult.FAILED)),
])
def test_run_test_schedule_entry(module, monkeypatch, results_filename, results_content, expected_results):
    with tempfile.TemporaryDirectory(prefix='test-schedule-sti') as tmpdir:
        # Prepare the module
        def run_playbook_mock(playbook_filepath, guest, inventory, cwd=None, json_output=False, log_filepath=None,
                              variables=None, ansible_playbook_filepath=None, extra_options=None):
            with open(os.path.join(cwd, results_filename), 'w') as file:
                file.write(results_content)

        module._config.update({
            'watch-timeout': 1
        })
        schedule_entry = TestScheduleEntry(
            gluetool.log.Logging().get_logger(),
            gluetool.utils.normalize_path(os.path.join(tmpdir, 'playbook1.yaml')),
            {}
        )
        schedule_entry.guest = NetworkedGuest(module, 'hostname', 'name')

        patch_shared(monkeypatch, module, {}, callables={
            'run_playbook': run_playbook_mock,
            'detect_ansible_interpreter': lambda _: None,
        })

        schedule_entry.runner_capability = 'sti'

        # Expected values in the output
        expected_task_name, expected_task_result, expected_schedule_entry_result = expected_results

        # Run the module - it creates new directories from the current working directory, temporarily change it to
        # the tmpdir so it gets cleaned up later
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            module.shared('run_test_schedule_entry', schedule_entry)
        finally:
            os.chdir(original_cwd)

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


@pytest.mark.parametrize('schedule_entry_results, expected_schedule_entry_outputs, expected_xml', [
    ([], [], None),
    (
        [TaskRun(name='foo', schedule_entry=None, result='fail', logs=None)],
        [
            TestScheduleEntryOutput(
                stage=TestScheduleEntryStage.RUNNING,
                label='ansible-output.txt',
                log_path='some/work-dirpath/ansible-output.txt',
                additional_data=None
            )
        ],
        bs4.BeautifulSoup(read_asset_file('results1.xml'), 'xml')
    ),
    (
        [TaskRun(name='foo', schedule_entry=None, result='error', logs=['log1', 'log2'])],
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
        bs4.BeautifulSoup(read_asset_file('results2.xml'), 'xml')
    )
])
def test_serialize_test_schedule_entry_results(module, schedule_entry_results,
                                               expected_schedule_entry_outputs, expected_xml):
    schedule_entry = TestScheduleEntry(
        gluetool.log.Logging().get_logger(),
        gluetool.utils.normalize_path('another/playbook2'),
        {}
    )
    schedule_entry.artifact_dirpath = 'some/artifact-dirpath'
    schedule_entry.work_dirpath = 'some/work-dirpath'
    schedule_entry.guest = NetworkedGuest(module, 'hostname', 'name')
    schedule_entry.testing_environment = schedule_entry.guest.environment = TestingEnvironment(arch='x86_64', compose='rhel-9')  # noqa
    schedule_entry.results = schedule_entry_results
    schedule_entry.runner_capability = 'sti'
    test_suite = gluetool.utils.new_xml_element('testsuite')

    assert str(test_suite) == '<testsuite/>'
    module.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

    assert schedule_entry.outputs == expected_schedule_entry_outputs

    if expected_xml:
        # Remove the first line from the parsed assets file. BeautifulSoup adds '<?xml version="1.0" encoding="utf-8"?>'
        # to the first line when parsing a file.
        assert test_suite.prettify() == '\n'.join(expected_xml.prettify().splitlines()[1:])
