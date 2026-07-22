# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import threading

import mock
import pytest

import gluetool_modules_framework.helpers.testing_farm_pipeline_stage_reporter

from gluetool import Failure
from gluetool_modules_framework.helpers.testing_farm_pipeline_stage_reporter import TestingFarmPipelineStageReporter
from gluetool_modules_framework.libs.pipeline_stages import SINGLEHOST_PER_PLAN_STAGES, MULTIHOST_PER_PLAN_STAGES
from . import create_module, patch_shared


COLDSTORE_URL = 'https://artifacts.example.com/abc123/'


@pytest.fixture(name='module')
def fixture_module():
    return create_module(TestingFarmPipelineStageReporter)[1]


@pytest.fixture(name='mock_request')
def fixture_mock_request():
    return mock.MagicMock()


@pytest.fixture(name='setup_shared')
def fixture_setup_shared(module, monkeypatch, mock_request):
    patch_shared(monkeypatch, module, {
        'coldstore_url': COLDSTORE_URL,
    }, callables={
        'testing_farm_request': lambda: mock_request,
        'register_event_handler': lambda event, handler, *args, **kwargs: None,
        'trigger_event': lambda event, *args, **kwargs: None,
    })

    return mock_request


@pytest.fixture(name='module_executed')
def fixture_module_executed(module, setup_shared):
    module.execute()
    setup_shared.reset_mock()
    return module


def _make_schedule_entry(plan='/plans/tier1', entry_id=None, work_dirpath=None):
    entry = mock.MagicMock()
    entry.plan = plan
    entry.id = entry_id or 'RHEL-9:x86_64:{}'.format(plan)
    entry.work_dirpath = work_dirpath
    return entry


def _find_stage(stages, stage, entry_id=None):
    for entry in stages:
        if entry['stage'] == stage and entry['id'] == entry_id:
            return entry
    return None


def _find_stages(stages, stage, plan=None):
    return [e for e in stages if e['stage'] == stage and (plan is None or e['plan'] == plan)]


# --- execute ---

def test_execute_initial_stages(module, setup_shared):
    module.execute()

    assert len(module._stages) == 4
    assert module._stages[0]['stage'] == 'initialization'
    assert module._stages[0]['status'] == 'running'
    assert module._stages[0]['started'] is not None
    assert module._stages[0]['plan'] is None
    assert module._stages[0]['id'] is None

    for name in ['test-discovery', 'testing', 'archiving']:
        entry = _find_stage(module._stages, name)
        assert entry is not None
        assert entry['status'] == 'pending'
        assert entry['started'] is None


def test_execute_sends_initial_update(module, setup_shared):
    module.execute()

    setup_shared.update.assert_called()
    call_kwargs = setup_shared.update.call_args[1]
    assert 'stages' in call_kwargs
    assert call_kwargs['artifacts_url'] == COLDSTORE_URL
    assert len(call_kwargs['stages']['pipeline']) == 4


# --- pipeline event handlers ---

@pytest.mark.parametrize('handler_name,event,stage,expected_status,check_started,check_finished', [
    ('_handle_initialization_finished', 'pipeline.initialization-finished',
     'initialization', 'finished', False, True),
    ('_handle_test_discovery_started', 'pipeline.test-discovery-started',
     'test-discovery', 'running', True, False),
    ('_handle_archiving_started', 'pipeline.archiving-started',
     'archiving', 'running', True, False),
    ('_handle_archiving_finished', 'pipeline.archiving-finished',
     'archiving', 'finished', False, True),
])
def test_pipeline_event_handler(
    module_executed, setup_shared,
    handler_name, event, stage, expected_status, check_started, check_finished
):
    if stage == 'archiving' and expected_status == 'finished':
        module_executed._handle_archiving_started('pipeline.archiving-started')
        setup_shared.reset_mock()

    handler = getattr(module_executed, handler_name)
    handler(event)

    entry = _find_stage(module_executed._stages, stage)
    assert entry['status'] == expected_status

    if check_started:
        assert entry['started'] is not None
    if check_finished:
        assert entry['finished'] is not None

    setup_shared.update.assert_called_once()


# --- schedule start ---

def test_schedule_start_with_discovery_events(module_executed, setup_shared):
    """Simulates tmt scheduler: init-finished and discovery-started fire before test-schedule.start."""
    module_executed._handle_initialization_finished('pipeline.initialization-finished')
    module_executed._handle_test_discovery_started('pipeline.test-discovery-started')
    setup_shared.reset_mock()

    schedule = [_make_schedule_entry()]
    module_executed._handle_schedule_start('test-schedule.start', schedule=schedule)

    init = _find_stage(module_executed._stages, 'initialization')
    assert init['status'] == 'finished'

    discovery = _find_stage(module_executed._stages, 'test-discovery')
    assert discovery['status'] == 'finished'
    assert discovery['started'] is not None
    assert discovery['finished'] is not None

    testing = _find_stage(module_executed._stages, 'testing')
    assert testing['status'] == 'running'


def test_schedule_start_without_discovery_events(module_executed, setup_shared):
    """Simulates non-tmt scheduler: no init/discovery events, test-schedule.start closes init as fallback."""
    schedule = [_make_schedule_entry()]
    module_executed._handle_schedule_start('test-schedule.start', schedule=schedule)

    init = _find_stage(module_executed._stages, 'initialization')
    assert init['status'] == 'finished'

    discovery = _find_stage(module_executed._stages, 'test-discovery')
    assert discovery['status'] == 'pending'

    testing = _find_stage(module_executed._stages, 'testing')
    assert testing['status'] == 'running'
    assert testing['started'] is not None


@pytest.mark.parametrize('runner,expected_stages', [
    ('test-schedule-runner', SINGLEHOST_PER_PLAN_STAGES),
    ('test-schedule-runner-multihost', MULTIHOST_PER_PLAN_STAGES),
    ('unknown-runner', SINGLEHOST_PER_PLAN_STAGES),
])
def test_schedule_start_adds_per_plan_stages(module_executed, setup_shared, runner, expected_stages):
    entry = _make_schedule_entry()
    module_executed._handle_schedule_start('test-schedule.start', schedule=[entry], runner=runner)

    for stage_name in expected_stages:
        found = _find_stage(module_executed._stages, stage_name, entry_id=entry.id)
        assert found is not None, 'Missing per-plan stage: {}'.format(stage_name)
        assert found['status'] == 'pending'
        assert found['plan'] == '/plans/tier1'
        assert found['id'] == entry.id
        assert found['workdir'] is None

    unexpected = set(SINGLEHOST_PER_PLAN_STAGES) - set(expected_stages)
    for stage_name in unexpected:
        assert _find_stage(module_executed._stages, stage_name, entry_id=entry.id) is None


def test_schedule_start_multiple_plans(module_executed, setup_shared):
    entries = [
        _make_schedule_entry(plan='/plans/tier1', entry_id='RHEL-9:x86_64:/plans/tier1'),
        _make_schedule_entry(plan='/plans/tier2', entry_id='RHEL-9:x86_64:/plans/tier2'),
    ]
    module_executed._handle_schedule_start('test-schedule.start', schedule=entries)

    for entry in entries:
        for stage_name in SINGLEHOST_PER_PLAN_STAGES:
            found = _find_stage(module_executed._stages, stage_name, entry_id=entry.id)
            assert found is not None


def test_schedule_start_per_plan_stages_include_workdir(module_executed, setup_shared):
    entry = _make_schedule_entry(work_dirpath='/var/tmp/tmt/plans/tier1')
    module_executed._handle_schedule_start('test-schedule.start', schedule=[entry])

    for stage_name in SINGLEHOST_PER_PLAN_STAGES:
        found = _find_stage(module_executed._stages, stage_name, entry_id=entry.id)
        assert found['workdir'] == '/var/tmp/tmt/plans/tier1'


def test_schedule_start_same_plan_multiple_environments(module_executed, setup_shared):
    entries = [
        _make_schedule_entry(plan='/plans/tier1', entry_id='RHEL-9:x86_64:/plans/tier1'),
        _make_schedule_entry(plan='/plans/tier1', entry_id='RHEL-9:aarch64:/plans/tier1'),
    ]
    module_executed._handle_schedule_start('test-schedule.start', schedule=entries)

    tier1_provisioning = _find_stages(module_executed._stages, 'provisioning', plan='/plans/tier1')
    assert len(tier1_provisioning) == 2

    for entry in entries:
        found = _find_stage(module_executed._stages, 'provisioning', entry_id=entry.id)
        assert found is not None
        assert found['plan'] == '/plans/tier1'


# --- per-plan events ---

@pytest.mark.parametrize('event,stage_name', [
    ('test-schedule.provisioning-started', 'provisioning'),
    ('test-schedule.guest-setup-started', 'guest-setup'),
    ('test-schedule.running-started', 'running'),
    ('test-schedule.cleanup-started', 'cleanup'),
    ('test-schedule.provisioning-finished', 'provisioning'),
    ('test-schedule.guest-setup-finished', 'guest-setup'),
    ('test-schedule.running-finished', 'running'),
    ('test-schedule.cleanup-finished', 'cleanup'),
])
def test_per_plan_event(module_executed, setup_shared, event, stage_name):
    entry = _make_schedule_entry()
    module_executed._handle_schedule_start('test-schedule.start', schedule=[entry])

    if event.endswith('-finished'):
        start_event = 'test-schedule.{}-started'.format(stage_name)
        module_executed._handle_per_plan_event(start_event, schedule_entry=entry)

    setup_shared.reset_mock()
    module_executed._handle_per_plan_event(event, schedule_entry=entry)

    found = _find_stage(module_executed._stages, stage_name, entry_id=entry.id)
    if event.endswith('-started'):
        assert found['status'] == 'running'
        assert found['started'] is not None
    else:
        assert found['status'] == 'finished'
        assert found['finished'] is not None

    setup_shared.update.assert_called_once()


def test_per_plan_event_no_schedule_entry(module_executed, setup_shared):
    module_executed._handle_per_plan_event('test-schedule.running-started')
    setup_shared.update.assert_not_called()


def test_per_plan_event_correct_entry_updated_with_multiple_environments(module_executed, setup_shared):
    entries = [
        _make_schedule_entry(plan='/plans/tier1', entry_id='RHEL-9:x86_64:/plans/tier1'),
        _make_schedule_entry(plan='/plans/tier1', entry_id='RHEL-9:aarch64:/plans/tier1'),
    ]
    module_executed._handle_schedule_start('test-schedule.start', schedule=entries)

    module_executed._handle_per_plan_event(
        'test-schedule.provisioning-started', schedule_entry=entries[0]
    )

    x86 = _find_stage(module_executed._stages, 'provisioning', entry_id=entries[0].id)
    aarch64 = _find_stage(module_executed._stages, 'provisioning', entry_id=entries[1].id)
    assert x86['status'] == 'running'
    assert aarch64['status'] == 'pending'


# --- schedule finished / error ---

def test_schedule_finished_closes_testing(module_executed, setup_shared):
    module_executed._handle_schedule_start('test-schedule.start', schedule=[_make_schedule_entry()])
    setup_shared.reset_mock()

    module_executed._handle_schedule_finished('test-schedule.finished')

    testing = _find_stage(module_executed._stages, 'testing')
    assert testing['status'] == 'finished'
    assert testing['finished'] is not None


def test_schedule_error_marks_running_stages_failed(module_executed, setup_shared):
    entry = _make_schedule_entry()
    module_executed._handle_schedule_start('test-schedule.start', schedule=[entry])
    module_executed._handle_per_plan_event('test-schedule.provisioning-started', schedule_entry=entry)
    setup_shared.reset_mock()

    module_executed._handle_schedule_error('test-schedule.error')

    provisioning = _find_stage(module_executed._stages, 'provisioning', entry_id=entry.id)
    assert provisioning['status'] == 'failed'
    assert provisioning['finished'] is not None

    testing = _find_stage(module_executed._stages, 'testing')
    assert testing['status'] == 'finished'


def test_schedule_error_leaves_pending_stages(module_executed, setup_shared):
    entry = _make_schedule_entry()
    module_executed._handle_schedule_start('test-schedule.start', schedule=[entry])
    module_executed._handle_schedule_error('test-schedule.error')

    for stage_name in ['provisioning', 'guest-setup', 'running', 'cleanup']:
        found = _find_stage(module_executed._stages, stage_name, entry_id=entry.id)
        assert found['status'] == 'pending'


# --- destroy ---

def test_destroy_archiving(module_executed, setup_shared):
    module_executed.destroy()
    setup_shared.update.assert_called_once()


def test_destroy_marks_running_per_plan_stages_failed(module_executed, setup_shared):
    entry = _make_schedule_entry()
    module_executed._handle_schedule_start('test-schedule.start', schedule=[entry])
    module_executed._handle_per_plan_event('test-schedule.running-started', schedule_entry=entry)
    setup_shared.reset_mock()

    module_executed.destroy()

    found = _find_stage(module_executed._stages, 'running', entry_id=entry.id)
    assert found['status'] == 'failed'


def test_destroy_closes_testing_if_running(module_executed, setup_shared):
    module_executed._handle_schedule_start(
        'test-schedule.start', schedule=[_make_schedule_entry()]
    )
    setup_shared.reset_mock()

    module_executed.destroy()

    testing = _find_stage(module_executed._stages, 'testing')
    assert testing['status'] == 'finished'


def test_destroy_systemexit_noop(module_executed, setup_shared):
    stages_before = list(module_executed._stages)
    module_executed.destroy(failure=Failure(module_executed, [None, SystemExit()]))
    assert module_executed._stages == stages_before
    setup_shared.update.assert_not_called()


def test_destroy_no_request_noop(module_executed, setup_shared):
    module_executed._request = None
    module_executed.destroy()
    setup_shared.update.assert_not_called()


# --- error resilience ---

def test_send_update_failure_does_not_propagate(module_executed, setup_shared):
    setup_shared.update.side_effect = Exception('API error')
    module_executed._handle_archiving_started('pipeline.archiving-started')


def test_handler_failure_does_not_propagate(module_executed, monkeypatch):
    patch_shared(monkeypatch, module_executed, {
        'testing_farm_request': None,
        'coldstore_url': COLDSTORE_URL,
    }, callables={
        'trigger_event': mock.MagicMock(side_effect=Exception('event error')),
    })
    module_executed._handle_schedule_start('test-schedule.start', schedule=[_make_schedule_entry()])


# --- thread safety ---

def test_concurrent_per_plan_events(module_executed, setup_shared):
    entries = [
        _make_schedule_entry(plan='/plans/plan{}'.format(i), entry_id='RHEL-9:x86_64:/plans/plan{}'.format(i))
        for i in range(10)
    ]
    module_executed._handle_schedule_start('test-schedule.start', schedule=entries)
    setup_shared.reset_mock()

    errors: List = []

    def fire_events(entry):
        try:
            module_executed._handle_per_plan_event('test-schedule.running-started', schedule_entry=entry)
            module_executed._handle_per_plan_event('test-schedule.running-finished', schedule_entry=entry)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=fire_events, args=(e,)) for e in entries]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors

    for entry in entries:
        found = _find_stage(module_executed._stages, 'running', entry_id=entry.id)
        assert found['status'] == 'finished'
