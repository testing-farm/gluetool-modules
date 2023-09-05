# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os

from mock import call, MagicMock

from dataclasses import dataclass

import gluetool

from gluetool.result import Ok

import gluetool_modules_framework.testing.test_schedule_runner

from gluetool_modules_framework.testing.test_schedule_tmt import TestScheduleEntry
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.guest_setup import GuestSetupOutput, GuestSetupStage
from gluetool_modules_framework.libs.test_schedule import (
    TestSchedule, TestScheduleResult as TSResult, TestScheduleEntryStage as TSEntryStage,
    TestScheduleEntryState as TSEntryState
)
from gluetool_modules_framework.helpers.rules_engine import RulesEngine

from . import create_module, patch_shared


ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


class GuestMock(MagicMock):
    def __init__(self, **kwargs):
        super(GuestMock, self).__init__(**kwargs)
        self.setup = MagicMock(return_value=Ok([
                GuestSetupOutput(
                    stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
                    label='guest setup',
                    log_path='log',
                    additional_data='data'
                )
            ]))
        self.destroy = MagicMock()


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.testing.test_schedule_runner.TestScheduleRunner)[1]


def create_test_schedule(entry_properties):
    schedule = TestSchedule()
    for stage, state, result in entry_properties:
        tec = TestingEnvironment(arch='x86_64', compose='Fedora37', excluded_packages=['excludes'])
        entry = TestScheduleEntry(
            gluetool.log.Logging.get_logger(),
            tec,
            'plan',
            'repodir'
        )
        entry.stage = stage
        entry.state = state
        entry.result = result
        entry.testing_environment = tec
        schedule.append(entry)
    return schedule


def evaluate_filter_mock(entries, context=None):
    return []


def test_execute_empty(module):
    with pytest.raises(gluetool.GlueError, match='no test schedule to run'):
        module.execute()


def test_execute(module, monkeypatch):
    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest_mock],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module.execute()

    guest_mock.destroy.assert_called_once_with()
    run_test_schedule_entry_mock.assert_called_once()
    assert len(test_schedule) == 1
    assert test_schedule[0].stage == TSEntryStage.COMPLETE
    assert test_schedule[0].state == TSEntryState.OK
    assert test_schedule[0].result == TSResult.UNDEFINED


def test_execute_destroy_if_fail(module, monkeypatch):
    module._config['reuse-guests'] = True
    module._config['destroy-if-fail'] = True
    module._config['max-parallel'] = 1
    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    test_schedule = create_test_schedule([
        (TSEntryStage.CREATED, TSEntryState.OK, TSResult.FAILED),
        (TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED),
        (TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)
    ])
    test_schedule_mock = MagicMock(return_value=test_schedule)
    run_test_schedule_entry_mock = MagicMock()
    provision_mock = MagicMock(return_value=[guest_mock])
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': test_schedule_mock,
        'evaluate_filter': evaluate_filter_mock,
        'provision': provision_mock,
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module.execute()

    provision_mock.call_args_list == [
        call(TestingEnvironment(arch='x86_64', compose='Fedora37')),
        call(TestingEnvironment(arch='x86_64', compose='Fedora37'))
    ]
    guest_mock.destroy.assert_has_calls([call(), call()])
    assert len(test_schedule) == 3

    for i in range(3):
        assert test_schedule[i].stage == TSEntryStage.COMPLETE
        assert test_schedule[i].state == TSEntryState.OK


def test_execute_reuse_guests(module, monkeypatch):
    module._config['reuse-guests'] = True
    module._config['max-parallel'] = 1
    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    test_schedule = create_test_schedule([
        (TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED),
        (TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED),
        (TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)
    ])
    run_test_schedule_entry_mock = MagicMock()
    provision_mock = MagicMock(return_value=[guest_mock])
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': provision_mock,
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module.execute()

    provision_mock.assert_called_once_with(TestingEnvironment(arch='x86_64', compose='Fedora37'))
    # The guest setup should be called only for the first schedule entry
    guest_mock.setup.assert_has_calls([
        call(stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION),
        call(stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION_WORKAROUNDS),
        call(stage=GuestSetupStage.ARTIFACT_INSTALLATION),
        call(stage=GuestSetupStage.POST_ARTIFACT_INSTALLATION_WORKAROUNDS),
        call(stage=GuestSetupStage.POST_ARTIFACT_INSTALLATION),
    ])
    guest_mock.destroy.assert_called_once_with()
    assert len(test_schedule) == 3

    for i in range(3):
        assert test_schedule[i].stage == TSEntryStage.COMPLETE
        assert test_schedule[i].state == TSEntryState.OK
        assert test_schedule[i].result == TSResult.UNDEFINED


def test_execute_provision_error(module, monkeypatch):
    def provision_error_mock(_):
        raise gluetool.GlueError('mocked provision error')

    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
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


def test_execute_setup_error(module, monkeypatch):
    def setup_error_mock(**kwargs):
        raise gluetool.GlueError('mocked guest setup error')

    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    guest_mock.setup = setup_error_mock

    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest_mock],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked guest setup error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_run_error(module, monkeypatch):
    def run_error_mock(_):
        raise gluetool.GlueError('mocked run error')

    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest_mock],
        'run_test_schedule_entry': run_error_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked run error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_cleanup_error(module, monkeypatch):
    def cleanup_error_mock():
        raise gluetool.GlueError('mocked cleanup error')

    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    guest_mock.destroy = cleanup_error_mock
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'provision': lambda _: [guest_mock],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked cleanup error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_schedule_entry_attribute_map(module, monkeypatch):
    rules_engine = create_module(RulesEngine)[1]
    guest_mock = GuestMock(
        hostname='foo',
        environment=TestingEnvironment(arch='x86_64', compose='Fedora37'),
        name='bar'
    )
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': rules_engine.evaluate_filter,
        'provision': lambda _: [guest_mock],
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module._config.update({
        'schedule-entry-attribute-map': os.path.join(ASSETS_DIR, 'test_schedule_runner', 'schedule-entry-attribute-map.yaml'),
    })
    module.execute()

    guest_mock.setup.assert_not_called()
    guest_mock.destroy.assert_not_called()
    run_test_schedule_entry_mock.assert_not_called()
    assert len(test_schedule) == 1
    assert test_schedule[0].stage == TSEntryStage.COMPLETE
    assert test_schedule[0].state == TSEntryState.OK
    assert test_schedule[0].result == TSResult.UNDEFINED
