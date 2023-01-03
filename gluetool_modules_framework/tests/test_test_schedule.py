# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
from mock import MagicMock
import gluetool
import gluetool_modules_framework.testing.test_schedule_runner
from gluetool.result import Ok
from gluetool.tests import NonLoadingGlue
from gluetool_modules_framework.libs.guest import Guest
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.guest_setup import GuestSetupOutput, GuestSetupStage
from gluetool_modules_framework.libs.test_schedule import (
    TestSchedule, TestScheduleResult as TSResult, TestScheduleEntryStage as TSEntryStage,
    TestScheduleEntryState as TSEntryState, TestScheduleEntry, InvalidTmtReferenceError,
    EmptyTestScheduleError, _env_to_str, _guest_to_str
)
from gluetool_modules_framework.helpers.rules_engine import RulesEngine
from . import create_module, patch_shared

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.testing.test_schedule_runner.TestScheduleRunner)[1]


@pytest.fixture(name='guest')
def fixture_guest():
    guest = MagicMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37', snapshots=True),
        name='bar',
        username='toor',
        setup=MagicMock(return_value=Ok([
            GuestSetupOutput(
                stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
                label='guest setup',
                log_path='log',
                additional_data='data'
            )
        ]))
    )
    return guest


@pytest.fixture(name='task')
def fixture_task():
    task = MagicMock()
    return task


def create_test_schedule(entry_properties):
    schedule = TestSchedule()
    for stage, state, result in entry_properties:
        entry = TestScheduleEntry(
            gluetool.log.Logging.get_logger(),
            'dummy_ID',
            'dummy_capability'
        )
        entry.stage = stage
        entry.state = state
        entry.result = result
        entry.testing_environment = TestingEnvironment(arch='x86_64', compose='Fedora37')
        schedule.append(entry)
    return schedule


def evaluate_filter_mock(entries, context=None):
    return []


def test_execute_empty(module):
    with pytest.raises(gluetool.GlueError, match='no test schedule to run'):
        module.execute()


def test_execute(module, monkeypatch, guest):
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module.execute()

    guest.destroy.assert_called_once_with()
    run_test_schedule_entry_mock.assert_called_once()
    assert len(test_schedule) == 1
    assert test_schedule[0].stage == TSEntryStage.COMPLETE
    assert test_schedule[0].state == TSEntryState.OK
    assert test_schedule[0].result == TSResult.UNDEFINED
    assert test_schedule[0].has_exceptions is False
    assert test_schedule[0].log_entry() is None


def test_execute_provision_error(module, monkeypatch):
    def provision_error_mock(_):
        raise gluetool.GlueError('mocked provision error')

    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': provision_error_mock,
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked provision error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_setup_error(module, monkeypatch, guest):
    def setup_error_mock(**kwargs):
        raise gluetool.GlueError('mocked guest setup error')

    guest.setup = setup_error_mock

    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked guest setup error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_run_error(module, monkeypatch, guest):
    def run_error_mock(_):
        raise gluetool.GlueError('mocked run error')

    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_error_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked run error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_cleanup_error(module, monkeypatch, guest):
    def cleanup_error_mock():
        raise gluetool.GlueError('mocked cleanup error')

    guest.destroy = cleanup_error_mock
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked cleanup error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_schedule_entry_attribute_map(module, monkeypatch, guest):
    rules_engine = create_module(RulesEngine)[1]
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': rules_engine.evaluate_filter,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module._config.update({
        'schedule-entry-attribute-map': os.path.join(ASSETS_DIR, 'test_schedule_runner', 'schedule-entry-attribute-map.yaml'),
    })
    module.execute()

    guest.setup.assert_not_called()
    guest.destroy.assert_not_called()
    run_test_schedule_entry_mock.assert_not_called()
    assert len(test_schedule) == 1
    assert test_schedule[0].stage == TSEntryStage.COMPLETE
    assert test_schedule[0].state == TSEntryState.OK
    assert test_schedule[0].result == TSResult.UNDEFINED


def test_log(module, monkeypatch, guest):
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module.execute()
    run_test_schedule_entry_mock.assert_called_once()
    assert len(test_schedule) == 1
    assert test_schedule.log(MagicMock, include_logs=True) is None
    assert test_schedule.log(MagicMock, include_connection_info=True) is None
    assert test_schedule.log(MagicMock, include_connection_info=True,
                             connection_info_docs_link="http://dummy.lan/docs") is None
    # The behaviour changes when there is no username.
    # TODO unit tests for logging
    del guest.username
    assert test_schedule.log(MagicMock, include_connection_info=True) is None


def test_env_to_str():
    env = TestingEnvironment(arch='x86_64', compose='Fedora37', snapshots=True)
    assert _env_to_str(env) == 'x86_64 Fedora37 S+'
    env = TestingEnvironment(arch='x86_64', compose='Fedora37')
    assert _env_to_str(env) == 'x86_64 Fedora37 S-'
    assert _env_to_str(None) == ''


def test_guest_to_str():
    guest = Guest(module=gluetool.Module(NonLoadingGlue(), 'foo'),
                  name='bar',
                  environment=TestingEnvironment(arch='x86_64', compose='Fedora37', snapshots=True))
    assert _guest_to_str(guest) == 'x86_64 Fedora37 S+\nbar'
    assert _guest_to_str(None) == ''


def test_empty_test_schedule_error(task):
    e = EmptyTestScheduleError(task)
    assert e.submit_to_sentry is False


def test_invalid_tmt_reference_error(task):
    e = InvalidTmtReferenceError(task, 'dummy')
    assert e.submit_to_sentry is False
