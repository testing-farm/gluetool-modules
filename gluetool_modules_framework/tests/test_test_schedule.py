# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
from mock import MagicMock
import gluetool
import gluetool_modules_framework.testing.test_schedule_runner
from gluetool.result import Ok
from gluetool.tests import NonLoadingGlue
from gluetool.log import LoggerMixin
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
        port='2222'
    )
    # For some reason, MagicMock doesn't set 'name'.
    guest.name = 'bar'

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


def _cut_up_log(log_records: str, index: int) -> tuple[list[str], list[str], list[str]]:
    print('log table cut up debug:')
    print(f'log count: {len(log_records)}')
    print(f'records:\n{log_records}')
    print(f'target record:\n{log_records[index]}')
    log_lines = str(log_records[index]).split('\n')
    print(f'log_lines:\n{log_lines}')
    log_headers = [line.strip() for line in log_lines[2].split('|') if line != '']
    print(f'log_headers:\n{log_headers}')
    log_cols = [line.strip() for line in log_lines[4].split('|') if line != '']
    if len(log_lines) > 5:
        # The guest name should be in the 6th col. See _guest_to_str(). A newline is inserted.
        guest_name = [line.strip() for line in log_lines[5].split('|') if line != '']
        if len(guest_name) > 6:
            guest_name = guest_name[5]
        else:
            guest_name = ''
    else:
        guest_name = ''
    print(f'log_cols:\n{log_cols}')
    print(f'guest_name:\n{guest_name}')
    return log_headers, log_cols, guest_name


def test_log(module, monkeypatch, log, guest):
    """
    The log table record looks like this and is not possible to use simple matching.
    <LogRecord: gluetool, 20, /home/siwalter/repo/testing-farm/gluetool-modules/port-libs-test_schedule/.tox/py39-unit-tests/lib/python3.9/site-packages/gluetool/log.py, 611, "test schedule:
    +----------+----------+---------+-----------+--------------------+--------------------+------------------+
    | SE       | Stage    | State   | Result    | Environment        | Guest              | Runner           |
    |----------+----------+---------+-----------+--------------------+--------------------+------------------|
    | dummy_ID | COMPLETE | OK      | UNDEFINED | x86_64 Fedora37 S- | x86_64 Fedora37 S+ | dummy_capability |
    |          |          |         |           |                    | bar                |                  |
    +----------+----------+---------+-----------+--------------------+--------------------+------------------+">

    Regex is fine, but rather let's cut it up and test for each part.
    """
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    assert guest.name == 'bar'
    module.execute()
    run_test_schedule_entry_mock.assert_called_once()

    # There should only be one entry.
    assert len(test_schedule) == 1
    # Prepare the logger.
    logger = gluetool.log.Logging.get_logger()
    # Clear Caplog of previous logs
    log.clear()
    # Log something
    assert test_schedule.log(logger.info, include_logs=True) is None
    log_headers, log_cols, guest_name = _cut_up_log(log.records, 1)
    assert log_headers == ['SE', 'Stage', 'State', 'Result', 'Environment', 'Guest', 'Runner']
    assert log_cols == ['dummy_ID', 'COMPLETE', 'OK', 'UNDEFINED', 'x86_64 Fedora37 S-', 'x86_64 Fedora37 S+',
                        'dummy_capability']
    assert guest_name == 'bar'
    assert str(log.records).find('http://dummy.lan/docs') == -1

    # Next test...
    log.clear()
    assert test_schedule.log(logger.info, include_connection_info=True) is None
    log_headers, log_cols, guest_name = _cut_up_log(log.records, 2)
    assert log_headers == ['SE', 'State', 'Result', 'Environment', 'SSH Command']
    assert log_cols == ['dummy_ID', 'OK', 'UNDEFINED', 'x86_64 Fedora37 S-', "ssh -l toor -p 2222 foo"]

    # Next test...
    log.clear()
    assert test_schedule.log(logger.info, include_connection_info=True,
                             connection_info_docs_link="http://dummy.lan/docs") is None
    assert str(log.records).find('http://dummy.lan/docs') != -1

    # Next test... The behaviour changes when there is no username.
    del guest.username
    log.clear()
    assert test_schedule.log(logger.info, include_connection_info=True) is None
    log_headers, log_cols, guest_name = _cut_up_log(log.records, 2)
    assert log_headers == ['SE', 'State', 'Result', 'Environment', 'SSH Command']
    assert log_cols == ['dummy_ID', 'OK', 'UNDEFINED', 'x86_64 Fedora37 S-', "not available"]


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
