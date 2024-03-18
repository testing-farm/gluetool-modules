# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os

from mock import MagicMock

import gluetool

from gluetool.result import Ok

import gluetool_modules_framework.testing.test_schedule_runner_multihost

from gluetool_modules_framework.testing.test_schedule_tmt_multihost import TestScheduleEntry
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
    return create_module(gluetool_modules_framework.testing.test_schedule_runner_multihost.TestScheduleRunnerMultihost)[1]


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
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': evaluate_filter_mock,
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module.execute()

    run_test_schedule_entry_mock.assert_called_once()
    assert len(test_schedule) == 1
    assert test_schedule[0].stage == TSEntryStage.COMPLETE
    assert test_schedule[0].state == TSEntryState.OK
    assert test_schedule[0].result == TSResult.UNDEFINED


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
        'provision': MagicMock(return_value=[guest_mock]),
        'run_test_schedule_entry': run_error_mock
    })
    with pytest.raises(gluetool.GlueError, match='mocked run error'):
        module.execute()
    assert test_schedule[0].state == TSEntryState.ERROR


def test_execute_schedule_entry_attribute_map(module, monkeypatch):
    rules_engine = create_module(RulesEngine)[1]
    test_schedule = create_test_schedule([(TSEntryStage.CREATED, TSEntryState.OK, TSResult.UNDEFINED)])
    run_test_schedule_entry_mock = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'test_schedule': lambda: test_schedule,
        'evaluate_filter': rules_engine.evaluate_filter,
        'run_test_schedule_entry': run_test_schedule_entry_mock
    })
    module._config.update({
        'schedule-entry-attribute-map': os.path.join(ASSETS_DIR, 'test_schedule_runner', 'schedule-entry-attribute-map.yaml'),
    })
    module.execute()

    run_test_schedule_entry_mock.assert_not_called()
    assert len(test_schedule) == 1
    assert test_schedule[0].stage == TSEntryStage.COMPLETE
    assert test_schedule[0].state == TSEntryState.OK
    assert test_schedule[0].result == TSResult.UNDEFINED


@pytest.mark.parametrize('option, expected', [
    ("10", 10),
    ("{{ MAX }}", 20)
], ids=['string', 'template'])
def test_parallel_limit(module, option, expected, monkeypatch):
    module._config['parallel-limit'] = option

    patch_shared(monkeypatch, module, {
        'eval_context': {
            'MAX': 20
        }
    })

    assert module.parallel_limit == expected
