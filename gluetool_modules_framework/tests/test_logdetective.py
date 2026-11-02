# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
from xml.etree import ElementTree

from mock import MagicMock, patch

import gluetool
import gluetool_modules_framework.helpers.logdetective
from gluetool_modules_framework.helpers.logdetective import (
    LogDetective, MAX_ARTIFACT_NAME_LENGTH, _sanitize_name, _truncate_name,
)
from gluetool_modules_framework.libs.results import AIAnalysis, AIAnalysisSnippet, Log, TestCase, TestSuite
from gluetool_modules_framework.libs.test_schedule import TestScheduleResult, TestScheduleEntryState
from gluetool_modules_framework.libs.test_schedule_tmt import TMT_LOG
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage

from . import create_module, check_loadable, patch_shared


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(LogDetective)[1]
    module._config['base-url'] = 'http://logdetective.test:8080'
    patch_shared(monkeypatch, module, {}, callables={'redact_text': lambda value: value})
    # Existing isolated analysis tests use synthetic URLs; readiness is covered separately below.
    monkeypatch.setattr(module, '_ready_files', lambda files: files)
    return module


def _make_schedule_entry(
        result=TestScheduleResult.FAILED,
        state=TestScheduleEntryState.OK,
        runner_capability='tmt',
        entry_id='entry-1',
        plan='/plans/basic',
        work_dirpath=None,
        results=None,
        error_message=None,
        error_stage=None,
        error_guest_setup_stage=None,
):
    se = MagicMock()
    se.id = entry_id
    se.result = result
    se.state = state
    se.runner_capability = runner_capability
    se.plan = plan
    se.work_dirpath = work_dirpath
    se.results = results or []
    se.error_message = error_message
    se.error_stage = error_stage
    se.error_guest_setup_stage = error_guest_setup_stage
    se.guest_setup_outputs = {}
    se.guest = None
    se.testing_environment = MagicMock(compose='Fedora-40', arch='x86_64')
    return se


def _make_suite(test_cases=None):
    suite = TestSuite(name='test-suite', result='failed')
    suite.test_cases = test_cases or []
    return suite


def _failed_tc(name='/test/fail', logs=None):
    return TestCase(name=name, result='failed', logs=logs or [])


def _passed_tc(name='/test/pass'):
    return TestCase(name=name, result='passed')


def _log(name, href=None):
    return Log(name=name, href=href if href is not None else 'http://artifacts/{}'.format(name))


_GOOD_RESPONSE = {
    'explanation': 'The test failed because of a missing dependency.',
    'solution': 'Install the missing package.',
    'no_issue_found': False,
    'snippets': [{'text': 'ImportError', 'line_number': 42, 'source_file': 'test.py'}],
}


# ---------------------------------------------------------------------------
# Basic sanity
# ---------------------------------------------------------------------------

def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/logdetective.py', 'LogDetective')


def test_execute_registers_api_token_secret(module, monkeypatch):
    """Execute registers the token after hide-secrets is available."""
    module._config['api-token'] = 'secret-token'

    secrets_added = []

    def fake_add_secrets(*args):
        secrets_added.extend(args)

    patch_shared(monkeypatch, module, {}, callables={'add_secrets': fake_add_secrets})

    module.sanity()
    assert not secrets_added
    module.execute()
    assert 'secret-token' in secrets_added
    assert module._registered_token == 'secret-token'


def test_execute_skips_when_no_token(module, monkeypatch):
    """Execute does nothing when api-token is not set."""
    module._config['api-token'] = None

    add_secrets_called = []

    def fake_add_secrets(*args):
        add_secrets_called.append(args)

    patch_shared(monkeypatch, module, {}, callables={'add_secrets': fake_add_secrets})

    module.execute()
    assert not add_secrets_called


def test_execute_skips_when_no_hide_secrets_shared(module, monkeypatch):
    """A configured token is never sent without add_secrets."""
    module._config['api-token'] = 'secret-token'

    # No add_secrets shared function available
    def fake_has_shared(name):
        return False

    monkeypatch.setattr(module, 'has_shared', fake_has_shared)

    request = MagicMock(side_effect=AssertionError('must not contact API'))
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests', request)
    module.execute()
    assert module._call_logdetective([{'name': 'f', 'content': 'x'}], {}) is None
    request.assert_not_called()


def test_execute_registration_failure_does_not_expose_token_or_send_request(module, monkeypatch):
    module._config['api-token'] = 'secret-token'
    patch_shared(monkeypatch, module, {}, callables={
        'add_secrets': MagicMock(side_effect=RuntimeError('secret-token')),
    })
    warning = MagicMock()
    monkeypatch.setattr(module, 'warn', warning)
    request = MagicMock(side_effect=AssertionError('must not contact API'))
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests', request)

    module.execute()
    assert module._call_logdetective([{'name': 'f', 'content': 'x'}], {}) is None
    assert module._registered_token is None
    assert all('secret-token' not in args[0] for args, _ in warning.call_args_list)
    request.assert_not_called()


@pytest.mark.parametrize('limit', [0, -1])
def test_sanity_rejects_nonpositive_artifact_limit(module, limit):
    module._config['artifact-limit'] = limit
    with pytest.raises(gluetool.GlueError, match='artifact-limit must be positive'):
        module.sanity()


def test_sanity_accepts_artifact_limit_above_default(module):
    module._config['artifact-limit'] = 20
    module.sanity()


# ---------------------------------------------------------------------------
# _sanitize_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('tmt-run.log', 'tmt-run.log'),
    ('/test/foo bar', '/test/foo bar'),   # slash and space are allowed
    ('/plans/basic::test', '/plans/basic__test'),  # colons replaced
    ('normal.txt', 'normal.txt'),
])
def test_sanitize_name(raw, expected):
    assert _sanitize_name(raw) == expected


# ---------------------------------------------------------------------------
# _truncate_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name,expected', [
    # Short name — unchanged.
    ('short.log', 'short.log'),
    # Exactly at the limit — unchanged.
    ('a' * MAX_ARTIFACT_NAME_LENGTH, 'a' * MAX_ARTIFACT_NAME_LENGTH),
    # Over the limit with an extension — stem truncated, extension preserved.
    ('a' * 300 + '.log', 'a' * (MAX_ARTIFACT_NAME_LENGTH - len('.log')) + '.log'),
    # Over the limit with no extension — entire name truncated.
    ('b' * 300, 'b' * MAX_ARTIFACT_NAME_LENGTH),
    # Extension alone longer than limit — treated as no extension, truncated whole.
    ('.' + 'c' * 300, ('.' + 'c' * 300)[:MAX_ARTIFACT_NAME_LENGTH]),
])
def test_truncate_name(name, expected):
    result = _truncate_name(name)
    assert result == expected
    assert len(result) <= MAX_ARTIFACT_NAME_LENGTH


# ---------------------------------------------------------------------------
# File gathering — plans with testcases
# ---------------------------------------------------------------------------

def test_build_files_testcase_only_tmt_log(module, tmp_path):
    """tmt-run.log is included as the first and only file when the testcase has no logs."""
    tmt_log = tmp_path / TMT_LOG
    tmt_log.write_text('log content', encoding='utf-8')

    se = _make_schedule_entry(work_dirpath=str(tmp_path))
    tc = _failed_tc('/test/foo', logs=[])

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as mock_artifacts_location:
        mock_artifacts_location.side_effect = lambda mod, path, logger: f'http://artifacts/{path}'
        files = module._build_files_for_testcase(se, tc)

    assert len(files) == 1
    assert files[0]['name'] == TMT_LOG
    assert files[0]['url'] == 'http://artifacts/{}'.format(tmt_log)


def test_build_files_uses_url_for_local_log(module, monkeypatch, tmp_path):
    """A local log is referenced by URL without reading or changing its contents."""
    original = 'prefix secret ' + 'x' * 100000 + ' secret suffix'
    path = tmp_path / TMT_LOG
    path.write_text(original, encoding='utf-8')
    patch_shared(monkeypatch, module, {}, callables={
        'redact_text': lambda value: value.replace('secret', 'hidden')
    })

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location',
               return_value='http://artifacts/tmt-run.log'):
        with patch('builtins.open', side_effect=AssertionError('local log opened')):
            files = module._build_files_for_testcase(_make_schedule_entry(work_dirpath=str(tmp_path)), _failed_tc())

    assert files == [{'name': TMT_LOG, 'url': 'http://artifacts/tmt-run.log'}]
    assert path.read_text(encoding='utf-8') == original


def test_build_files_uses_url_when_redactor_unavailable(module, monkeypatch, tmp_path):
    """Local log collection does not depend on a redactor."""
    path = tmp_path / TMT_LOG
    path.write_text('secret', encoding='utf-8')
    monkeypatch.delitem(module.glue.pipelines[-1].shared_functions, 'redact_text')

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location',
               return_value='http://artifacts/tmt-run.log'):
        files = module._build_files_for_testcase(
            _make_schedule_entry(work_dirpath=str(tmp_path)), _failed_tc())

    assert files == [{'name': TMT_LOG, 'url': 'http://artifacts/tmt-run.log'}]


def test_build_files_uses_url_when_redaction_fails(module, monkeypatch, tmp_path):
    """Local log collection does not call the redactor."""
    path = tmp_path / TMT_LOG
    path.write_text('secret', encoding='utf-8')

    def failed_redaction(value):
        raise ValueError('redaction failed')

    patch_shared(monkeypatch, module, {}, callables={'redact_text': failed_redaction})
    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location',
               return_value='http://artifacts/tmt-run.log'):
        files = module._build_files_for_testcase(
            _make_schedule_entry(work_dirpath=str(tmp_path)), _failed_tc())

    assert files == [{'name': TMT_LOG, 'url': 'http://artifacts/tmt-run.log'}]


def test_build_files_per_test_logs_included(module, tmp_path):
    """The testcase's own logs are appended after tmt-run.log, reusing their hrefs verbatim."""
    tmt_log = tmp_path / TMT_LOG
    tmt_log.write_text('plan log', encoding='utf-8')

    tc = _failed_tc('/test/bar', logs=[_log('testout.log', href='http://artifacts/testout.log')])
    se = _make_schedule_entry(work_dirpath=str(tmp_path))

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as mock_artifacts_location:
        mock_artifacts_location.side_effect = lambda mod, path, logger: f'http://artifacts/{path}'
        files = module._build_files_for_testcase(se, tc)

    assert len(files) == 2
    assert files[0]['name'] == TMT_LOG
    assert files[1]['name'] == 'testout.log'
    # The testcase log's href is used as-is, not re-derived through artifacts_location.
    assert files[1]['url'] == 'http://artifacts/testout.log'


def test_build_files_unique_names(module, tmp_path):
    """Duplicate log names on a testcase are de-duplicated."""
    (tmp_path / TMT_LOG).write_text('x', encoding='utf-8')

    tc = _failed_tc('/test/a', logs=[
        _log('testout.log', href='http://artifacts/a.log'),
        _log('testout.log', href='http://artifacts/b.log'),
    ])

    se = _make_schedule_entry(work_dirpath=str(tmp_path))
    files = module._build_files_for_testcase(se, tc)
    names = [f['name'] for f in files]
    assert len(set(names)) == len(names), 'Duplicate names: {}'.format(names)


def test_build_files_capped_at_15(module, tmp_path):
    """File list is capped at 15 entries including tmt-run.log."""
    (tmp_path / TMT_LOG).write_text('log', encoding='utf-8')

    tc = _failed_tc('/test/big', logs=[
        _log('out{}.log'.format(i), href='http://artifacts/out{}.log'.format(i)) for i in range(20)
    ])

    se = _make_schedule_entry(work_dirpath=str(tmp_path))
    files = module._build_files_for_testcase(se, tc)
    assert len(files) <= 15
    assert files[0]['name'] == TMT_LOG


def test_build_files_missing_tmt_log(module, tmp_path):
    """Missing tmt-run.log is handled gracefully; the testcase's own logs are still included."""
    tc = _failed_tc('/test/x', logs=[_log('testout.log', href='http://artifacts/testout.log')])

    se = _make_schedule_entry(work_dirpath=str(tmp_path))
    files = module._build_files_for_testcase(se, tc)
    assert all(f['name'] != TMT_LOG for f in files)
    assert len(files) == 1


def test_build_files_no_work_dirpath_skips_tmt_log(module):
    """When work_dirpath is missing, tmt-run.log is skipped but the testcase logs are kept."""
    tc = _failed_tc('/test/foo', logs=[_log('testout.log', href='http://artifacts/testout.log')])

    se = _make_schedule_entry(work_dirpath=None)
    files = module._build_files_for_testcase(se, tc)
    assert len(files) == 1
    assert files[0]['name'] == 'testout.log'


def test_build_files_uses_only_regular_source_artifacts(module, tmp_path):
    """Directory links remain in xUnit but are not submitted to Log Detective."""
    folder = tmp_path / 'output'
    folder.mkdir()
    log_path = folder / 'testout.log'
    log_path.write_text('failure', encoding='utf-8')
    task = MagicMock(name='task')
    task.name = '/test/fail'
    task.serial_number = 1
    task.guest = None
    task.artifacts = [MagicMock(name='directory'), MagicMock(name='log')]
    task.artifacts[0].name = 'log_dir'
    task.artifacts[0].path = str(folder)
    task.artifacts[1].name = 'testout.log'
    task.artifacts[1].path = str(log_path)
    se = _make_schedule_entry(results=[task])
    tc = _failed_tc(logs=[_log('log_dir'), _log('testout.log')])
    tc.serial_number = 1

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as location:
        location.side_effect = lambda mod, path, logger: 'http://artifacts/' + path
        files = module._build_files_for_testcase(se, tc)

    assert [entry['name'] for entry in files] == ['testout.log']
    assert files[0]['url'] == 'http://artifacts/' + str(log_path)


def test_build_files_deduplicates_results_missing_tmt_log(module, tmp_path):
    """RESULTS_MISSING names tmt-run.log as testout.log; submit the underlying file once."""
    tmt_log = tmp_path / TMT_LOG
    tmt_log.write_text('tmt failed before results', encoding='utf-8')
    task = MagicMock()
    task.name = '/test/fail'
    task.serial_number = None
    task.guest = None
    task.artifacts = [MagicMock(name='testout')]
    task.artifacts[0].name = 'testout.log'
    task.artifacts[0].path = str(tmt_log)
    se = _make_schedule_entry(work_dirpath=str(tmp_path), results=[task])

    files = module._build_files_for_testcase(se, _failed_tc())

    assert len(files) == 1
    assert files[0]['name'] == TMT_LOG


def test_build_files_keeps_main_output_with_many_artifacts(module, tmp_path):
    """The 15-file API limit must not discard the failing testcase's main output."""
    task = MagicMock()
    task.name = '/test/fail'
    task.serial_number = 1
    task.guest = None
    task.artifacts = []
    for name in ['aux-{:02d}.log'.format(index) for index in range(20)] + ['testout.log']:
        path = tmp_path / name
        path.write_text('log', encoding='utf-8')
        artifact = MagicMock()
        artifact.name = name
        artifact.path = str(path)
        task.artifacts.append(artifact)
    se = _make_schedule_entry(results=[task])
    tc = _failed_tc()
    tc.serial_number = 1

    files = module._build_files_for_testcase(se, tc)
    assert len(files) == 15
    assert files[0]['name'] == 'testout.log'


@pytest.mark.parametrize('limit,expected', [
    (1, ['testout.log']),
    (2, ['testout.log', 'journal.txt']),
    (20, None),
])
def test_build_files_respects_configured_artifact_limit(module, tmp_path, limit, expected):
    module._config['artifact-limit'] = limit
    (tmp_path / TMT_LOG).write_text('plan log', encoding='utf-8')
    tc = _failed_tc(logs=[_log('aux-{}.log'.format(i)) for i in range(20)] +
                    [_log('journal.txt'), _log('testout.log')])
    se = _make_schedule_entry(work_dirpath=str(tmp_path))

    files = module._build_files_for_testcase(se, tc)
    assert len(files) == limit
    if expected is not None:
        assert [entry['name'] for entry in files] == expected
    else:
        assert files[0]['name'] == TMT_LOG
        assert 'testout.log' in [entry['name'] for entry in files]


def test_build_files_testcase_no_logs_no_work_dirpath_empty(module):
    """A testcase with no logs and no work directory yields an empty file list."""
    se = _make_schedule_entry(work_dirpath=None)
    files = module._build_files_for_testcase(se, _failed_tc('/test/foo', logs=[]))
    assert files == []


# ---------------------------------------------------------------------------
# File gathering — suite fallback (infrastructure failures)
# ---------------------------------------------------------------------------

def test_build_files_suite_fallback_with_failing_stage(module, tmp_path):
    """Suite fallback includes the log from the specific failing guest-setup stage."""
    from gluetool_modules_framework.libs.guest_setup import GuestSetupOutput

    stage_log = tmp_path / 'stage.log'
    stage_log.write_text('setup failed here', encoding='utf-8')

    output = GuestSetupOutput(
        stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        label='setup-stage.log',
        log_path=str(stage_log),
        additional_data={}
    )

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION
    )
    se.guest_setup_outputs = {GuestSetupStage.PRE_ARTIFACT_INSTALLATION: [output]}

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as mock_artifacts_location:
        mock_artifacts_location.side_effect = lambda mod, path, logger: f'http://artifacts/{path}'
        files = module._build_files_for_suite_fallback(se)

    assert len(files) == 1
    assert files[0]['name'] == 'setup-stage.log'
    assert files[0]['url'] == 'http://artifacts/{}'.format(stage_log)


def test_build_files_suite_fallback_all_stages_when_no_specific_failure(module, tmp_path):
    """When no specific failing stage, all guest-setup logs are included in order."""
    from gluetool_modules_framework.libs.guest_setup import GuestSetupOutput

    log1 = tmp_path / 'stage1.log'
    log1.write_text('stage1', encoding='utf-8')
    log2 = tmp_path / 'stage2.log'
    log2.write_text('stage2', encoding='utf-8')

    out1 = GuestSetupOutput(
        stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        label='stage1.log',
        log_path=str(log1),
        additional_data={}
    )
    out2 = GuestSetupOutput(
        stage=GuestSetupStage.ARTIFACT_INSTALLATION,
        label='stage2.log',
        log_path=str(log2),
        additional_data={}
    )

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=None
    )
    # Populate multiple stages - keys must be GuestSetupStage enum values
    se.guest_setup_outputs = {
        GuestSetupStage.PRE_ARTIFACT_INSTALLATION: [out1],
        GuestSetupStage.ARTIFACT_INSTALLATION: [out2],
    }

    files = module._build_files_for_suite_fallback(se)
    # Both logs included
    assert len(files) == 2
    names = [f['name'] for f in files]
    assert 'stage1.log' in names
    assert 'stage2.log' in names


def test_build_files_suite_fallback_guest_event_log(module, tmp_path):
    """Guest event log is included when available."""
    from gluetool_modules_framework.provision.artemis import ArtemisGuest

    event_log = tmp_path / 'guest-event.log'
    event_log.write_text('provisioning events', encoding='utf-8')

    guest = MagicMock(spec=ArtemisGuest)
    guest.event_log_path = str(event_log)
    guest.guest_logs = []

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=None
    )
    se.guest = guest

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as mock_artifacts_location:
        mock_artifacts_location.side_effect = lambda mod, path, logger: f'http://artifacts/{path}'
        files = module._build_files_for_suite_fallback(se)

    assert any(f['name'] == 'guest-event.log' and f['url'] == 'http://artifacts/{}'.format(event_log)
               for f in files)


def test_build_files_suite_fallback_artemis_console_logs(module, tmp_path):
    """Artemis guest console logs are included."""
    from gluetool_modules_framework.provision.artemis import ArtemisGuestLog, ArtemisGuest

    console_log = tmp_path / 'console-guest123.log'
    console_log.write_text('console output', encoding='utf-8')

    guest_log = ArtemisGuestLog(
        name='console.log',
        type='blob',
        filename='console-{guestname}.log',
        datetime_filename='console-{guestname}-{datetime}.log',
        content="CONTENT",
        save_empty=False
    )

    guest = MagicMock(spec=ArtemisGuest)
    guest.artemis_id = 'guest123'
    guest.event_log_path = None
    guest.guest_logs = [guest_log]

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=None
    )
    se.guest = guest

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as mock_artifacts_location:
        mock_artifacts_location.side_effect = lambda mod, path, logger: f'http://artifacts/{path}'
        files = module._build_files_for_suite_fallback(se)

    assert any(f['name'] == 'console.log' and f['url'] == 'http://artifacts/{}'.format(console_log)
               for f in files)


def test_build_files_suite_fallback_skips_empty_guest_logs(module, tmp_path):
    """Empty guest logs are skipped unless save_empty is True."""
    from gluetool_modules_framework.provision.artemis import ArtemisGuestLog

    # Create a log file but mark it as empty content with save_empty=False
    guest_log = ArtemisGuestLog(
        name='empty.log',
        type='blob',
        filename='empty-{guestname}.log',
        datetime_filename='empty-{guestname}-{datetime}.log',
        content=None,  # marked as having no content
        save_empty=False
    )

    guest = MagicMock()
    guest.artemis_id = 'guest123'
    guest.event_log_path = None
    guest.guest_logs = [guest_log]

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=None
    )
    se.guest = guest

    files = module._build_files_for_suite_fallback(se)
    # Empty log should be skipped
    assert not any(f['name'] == 'empty.log' for f in files)


def test_build_files_suite_fallback_tmt_run_log_if_exists(module, tmp_path):
    """tmt-run.log is included if it was written before the crash."""
    tmt_log = tmp_path / TMT_LOG
    tmt_log.write_text('partial log', encoding='utf-8')

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=None
    )

    with patch('gluetool_modules_framework.helpers.logdetective.artifacts_location') as mock_artifacts_location:
        mock_artifacts_location.side_effect = lambda mod, path, logger: f'http://artifacts/{path}'
        files = module._build_files_for_suite_fallback(se)

    assert any(f['name'] == TMT_LOG and f['url'] == 'http://artifacts/{}'.format(tmt_log)
               for f in files)


def test_build_files_suite_fallback_synthetic_error(module):
    """When no logs exist, a synthetic error.txt is created from schedule entry metadata."""
    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=None,
        error_stage=MagicMock(value='execute'),
        error_guest_setup_stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        error_message='Network timeout'
    )

    files = module._build_files_for_suite_fallback(se)
    assert len(files) == 1
    assert files[0]['name'] == 'error.txt'
    content = files[0]['content']
    assert 'execute' in content
    assert 'pre-artifact-installation' in content
    assert 'Network timeout' in content


def test_build_files_suite_fallback_no_logs_no_error_metadata(module):
    """When no logs and no error metadata, suite fallback returns empty and logs a warning."""
    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=None,
        error_stage=None,
        error_guest_setup_stage=None,
        error_message=None
    )

    files = module._build_files_for_suite_fallback(se)
    assert files == []


def test_build_files_suite_fallback_capped_at_15(module, tmp_path):
    """Suite fallback respects the 15-file cap."""
    from gluetool_modules_framework.libs.guest_setup import GuestSetupOutput

    # Create 20 stage logs
    outputs = []
    for i in range(20):
        log = tmp_path / f'stage{i}.log'
        log.write_text(f'content{i}', encoding='utf-8')
        outputs.append(GuestSetupOutput(
            stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
            label=f'stage{i}.log',
            log_path=str(log),
            additional_data={}
        ))

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=str(tmp_path),
        error_guest_setup_stage=None
    )
    se.guest_setup_outputs = {GuestSetupStage.PRE_ARTIFACT_INSTALLATION: outputs}

    files = module._build_files_for_suite_fallback(se)
    assert len(files) <= 15


@pytest.mark.parametrize('limit', [1, 20])
def test_build_files_suite_fallback_respects_configured_artifact_limit(module, tmp_path, limit):
    from gluetool_modules_framework.libs.guest_setup import GuestSetupOutput

    module._config['artifact-limit'] = limit
    outputs = []
    for i in range(20):
        path = tmp_path / 'stage{}.log'.format(i)
        path.write_text('failure', encoding='utf-8')
        outputs.append(GuestSetupOutput(
            stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
            label=path.name,
            log_path=str(path),
            additional_data={}
        ))
    se = _make_schedule_entry(state=TestScheduleEntryState.ERROR)
    se.guest_setup_outputs = {GuestSetupStage.PRE_ARTIFACT_INSTALLATION: outputs}

    assert len(module._build_files_for_suite_fallback(se)) == limit


def test_build_files_suite_fallback_no_work_dirpath_skips_guest_logs(module):
    """When work_dirpath is missing, guest console logs are skipped with a warning."""
    from gluetool_modules_framework.provision.artemis import ArtemisGuestLog

    guest_log = ArtemisGuestLog(
        name='console.log',
        type='blob',
        filename='console-{guestname}.log',
        datetime_filename='console-{guestname}-{datetime}.log',
        content="CONTENT",
        save_empty=False
    )

    guest = MagicMock()
    guest.artemis_id = 'guest123'
    guest.event_log_path = None
    guest.guest_logs = [guest_log]

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        work_dirpath=None,  # no work directory
        error_guest_setup_stage=None
    )
    se.guest = guest

    files = module._build_files_for_suite_fallback(se)
    # Guest console logs should be skipped
    assert not any(f['name'] == 'console.log' for f in files)


# ---------------------------------------------------------------------------
# build_metadata
# ---------------------------------------------------------------------------

def test_build_metadata_infra_error(module):
    """infra_status reflects an infrastructure failure."""
    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        error_message='Provisioning timed out',
        error_stage=MagicMock(value='guest-setup'),
    )
    meta = module._build_metadata(se)
    assert 'Infrastructure' in meta['infra_status']
    assert 'Provisioning timed out' in meta['infra_status']


def test_build_metadata_tmt_failure(module):
    """infra_status reflects a successful infra when tmt ran to completion."""
    se = _make_schedule_entry(state=TestScheduleEntryState.OK)
    meta = module._build_metadata(se)
    assert 'provisioned' in meta['infra_status'].lower()


def test_build_metadata_commentary_contains_plan(module, monkeypatch):
    """Commentary includes the plan name."""
    patch_shared(monkeypatch, module, {'primary_task': None})
    se = _make_schedule_entry(plan='/plans/ci')
    meta = module._build_metadata(se)
    assert '/plans/ci' in (meta['commentary'] or '')


def test_build_metadata_primary_task_exception_handled(module, monkeypatch):
    """Exception when retrieving primary_task is caught and logged."""
    def raise_error():
        raise RuntimeError('primary_task unavailable')

    patch_shared(monkeypatch, module, {})
    monkeypatch.setattr(module, 'shared', raise_error)

    se = _make_schedule_entry(plan='/plans/test')
    # Should not raise; exception is caught and logged
    meta = module._build_metadata(se)
    assert meta['commentary'] is not None  # plan name should still be included


def test_build_metadata_error_guest_setup_stage_in_infra_status(module):
    """infra_status includes error_guest_setup_stage when set."""
    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        error_guest_setup_stage=GuestSetupStage.PRE_ARTIFACT_INSTALLATION,
        error_stage=None,
        error_message=None
    )
    meta = module._build_metadata(se)
    assert 'pre-artifact-installation' in meta['infra_status']


def test_build_metadata_with_primary_task(module, monkeypatch):
    """Commentary includes primary_task when available."""
    mock_task = MagicMock()
    mock_task.__str__ = MagicMock(return_value='my-package-1.0-1.fc39')

    patch_shared(monkeypatch, module, {'primary_task': mock_task})

    se = _make_schedule_entry(plan='/plans/test')
    meta = module._build_metadata(se)
    assert meta['commentary'] is not None
    assert 'my-package-1.0-1.fc39' in meta['commentary']


# ---------------------------------------------------------------------------
# HTTP / Log Detective call
# ---------------------------------------------------------------------------

def _mock_response(data):
    resp = MagicMock()
    resp.status_code = 202
    resp.ok = True
    resp.headers = {'Location': '/tasks/test-task', 'Retry-After': '0'}
    resp.json.return_value = {
        'id': 'test-task',
        'taskType': 'generic',
        'createdAt': '2026-09-18T10:00:00Z',
        'status': 'done',
        'error': None,
        'result': data,
    }
    return resp


def test_call_logdetective_success(module, monkeypatch):
    """A successful response is parsed into an AIAnalysis structure."""
    mock_wait = MagicMock(return_value=_mock_response(_GOOD_RESPONSE))
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', mock_wait)

    result = module._call_logdetective([{'name': 'tmt-run.log', 'content': 'x'}], {})

    assert result is not None
    assert isinstance(result, AIAnalysis)
    assert result.explanation == _GOOD_RESPONSE['explanation']
    assert result.solution == _GOOD_RESPONSE['solution']
    assert result.no_issue_found is False
    assert len(result.snippets) == 1
    assert result.snippets[0].text == 'ImportError'
    assert result.snippets[0].line_number == 42
    assert result.snippets[0].source_file == 'test.py'


def test_call_logdetective_redacts_request_before_submission(module, monkeypatch):
    """Inline content, names and metadata are sanitized before the HTTP wrapper sees them."""
    patch_shared(monkeypatch, module, {}, callables={
        'redact_text': lambda value: value.replace('secret', 'hidden')
    })
    captured = {}

    class FakeRequests:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, json=None, headers=None, timeout=None):
            captured.update(json)
            return _mock_response(_GOOD_RESPONSE)

    def fake_wait(label, fn, timeout, tick):
        return fn().value

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests',
                        lambda logger: FakeRequests())

    files = [{'name': 'secret.log', 'content': 'a secret in the log'},
             {'name': 'hidden.log', 'content': 'another log'},
             {'name': 'remote.log', 'url': 'https://artifacts/secret'}]
    metadata = {'commentary': 'plan secret', 'infra_status': None}
    assert module._call_logdetective(files, metadata) is not None
    assert captured['files'] == [{'name': 'hidden.log', 'content': 'a hidden in the log'},
                                 {'name': 'hidden_1.log', 'content': 'another log'}]
    assert captured['build_metadata'] == {'commentary': 'plan hidden', 'infra_status': None}
    assert files[0]['content'] == 'a secret in the log'
    assert metadata['commentary'] == 'plan secret'


def test_call_logdetective_no_explanation(module, monkeypatch):
    """A response with no explanation text returns None."""
    mock_wait = MagicMock(return_value=_mock_response({'explanation': '', 'no_issue_found': True}))
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', mock_wait)

    result = module._call_logdetective([{'name': 'tmt-run.log', 'content': 'x'}], {})
    assert result is None


def test_call_logdetective_skips_explanation_cleaned_to_empty(module, monkeypatch):
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait',
                        MagicMock(return_value=_mock_response({'explanation': '\x00\x01\ud800'})))
    assert module._call_logdetective([{'name': 'f', 'content': 'x'}], {}) is None


def test_call_logdetective_auth_header_when_token_set(module, monkeypatch):
    """Authorization header is included when api-token is configured."""
    module._config['api-token'] = 'secret-tok'
    patch_shared(monkeypatch, module, {}, callables={'add_secrets': lambda token: None})
    module.execute()

    captured_headers = {}

    def fake_wait(label, fn, timeout, tick):
        class FakeRequests:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def request(self, method, url, json=None, headers=None, timeout=None):
                captured_headers.update(headers or {})
                resp = MagicMock()
                resp.json.return_value = _GOOD_RESPONSE
                return resp

        with patch('gluetool_modules_framework.helpers.logdetective.requests', return_value=FakeRequests()):
            return fn().value

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert 'Authorization' in captured_headers
    assert captured_headers['Authorization'] == 'Bearer secret-tok'


def test_call_logdetective_no_auth_header_without_token(module, monkeypatch):
    """Authorization header is omitted when api-token is not set."""
    captured_headers = {}

    def fake_wait(label, fn, timeout, tick):
        class FakeRequests:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def request(self, method, url, json=None, headers=None, timeout=None):
                captured_headers.update(headers or {})
                resp = MagicMock()
                resp.json.return_value = _GOOD_RESPONSE
                return resp

        with patch('gluetool_modules_framework.helpers.logdetective.requests', return_value=FakeRequests()):
            return fn().value

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert 'Authorization' not in captured_headers


def test_call_logdetective_request_failure_returns_none(module, monkeypatch):
    """A failing request returns None without raising."""
    monkeypatch.setattr(
        gluetool_modules_framework.helpers.logdetective,
        'wait',
        MagicMock(side_effect=Exception('timeout'))
    )
    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None


def test_call_logdetective_trips_circuit_breaker_on_exhausted_retries(module, monkeypatch):
    """When the retry budget is exhausted, the endpoint is marked unavailable for the whole run."""
    mock_wait = MagicMock(side_effect=Exception('timeout'))
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', mock_wait)

    assert not module._endpoint_unavailable

    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None
    assert module._endpoint_unavailable


def test_call_logdetective_skips_when_endpoint_unavailable(module, monkeypatch):
    """Once the circuit breaker is tripped, subsequent calls short-circuit without contacting wait()."""
    module._endpoint_unavailable = True

    mock_wait = MagicMock()
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', mock_wait)

    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None
    mock_wait.assert_not_called()


def test_call_logdetective_client_error_returns_none(module, monkeypatch):
    """A permanent 4xx response stops retrying and returns None."""
    def fake_wait(label, fn, timeout, tick):
        class FakeRequests:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def request(self, method, url, json=None, headers=None, timeout=None):
                resp = MagicMock()
                resp.ok = False
                resp.status_code = 400
                resp.text = 'Bad Request'
                return resp

        with patch('gluetool_modules_framework.helpers.logdetective.requests', return_value=FakeRequests()):
            result = fn()
            # 4xx: _do_request returns Result.Ok(resp) to stop retrying; value is the error response
            assert result.is_ok
            return result.value

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None


def test_call_logdetective_server_error_retries(module, monkeypatch):
    """A 5xx response causes _do_request to return Result.Error so wait() can retry."""
    def fake_wait(label, fn, timeout, tick):
        class FakeRequests:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def request(self, method, url, json=None, headers=None, timeout=None):
                resp = MagicMock()
                resp.ok = False
                resp.status_code = 503
                resp.text = 'Service Unavailable'
                return resp

        with patch('gluetool_modules_framework.helpers.logdetective.requests', return_value=FakeRequests()):
            result = fn()
            # 5xx: _do_request returns Result.Error to signal retry
            assert not result.is_ok
            raise Exception('retry exhausted')

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None


def test_call_logdetective_rate_limit_retries(module, monkeypatch):
    """A 429 response is treated as transient and causes _do_request to signal retry."""
    def fake_wait(label, fn, timeout, tick):
        class FakeRequests:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def request(self, method, url, json=None, headers=None, timeout=None):
                resp = MagicMock()
                resp.ok = False
                resp.status_code = 429
                resp.text = 'Too Many Requests'
                return resp

        with patch('gluetool_modules_framework.helpers.logdetective.requests', return_value=FakeRequests()):
            result = fn()
            # 429: _do_request returns Result.Error to signal retry
            assert not result.is_ok
            raise Exception('retry exhausted')

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None


def test_call_logdetective_dryrun_skips_request(module, monkeypatch):
    """In dry-run mode, the HTTP request is skipped and None is returned."""
    # Enable dry-run mode
    monkeypatch.setattr(module.glue, '_dryrun_level', gluetool.glue.DryRunLevels.DRY)

    # wait should not be called in dry-run mode
    wait_called = []

    def fake_wait(*args, **kwargs):
        wait_called.append(True)
        return MagicMock()

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)

    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None
    assert not wait_called


def test_call_logdetective_non_json_response(module, monkeypatch):
    """Non-JSON response is handled gracefully and returns None."""
    def fake_wait(label, fn, timeout, tick):
        resp = MagicMock()
        resp.ok = True
        resp.json.side_effect = ValueError('Invalid JSON')
        return resp

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)

    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None


def test_call_logdetective_request_exception(module, monkeypatch):
    """Exception during request is caught and returns None."""
    def fake_wait(label, fn, timeout, tick):
        class FakeRequests:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def request(self, method, url, json=None, headers=None, timeout=None):
                raise ConnectionError('Network error')

        with patch('gluetool_modules_framework.helpers.logdetective.requests', return_value=FakeRequests()):
            result = fn()
            # The request error is retried by wait(); exhaustion raises to the caller.
            assert not result.is_ok
            raise Exception('retry exhausted')

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    result = module._call_logdetective([{'name': 'f', 'content': 'x'}], {})
    assert result is None


def test_plan_timeout_is_shared_between_testcases_but_not_plans(module, monkeypatch):
    """A busy plan stops at its deadline and the next plan gets a fresh budget."""
    module._config['plan-timeout'] = 180
    clock = {'now': 0.0}
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    attempted = []

    def fake_analyze(entry, test_case):
        if module._remaining_budget() <= 0:
            return None
        attempted.append((entry.plan, test_case.name))
        clock['now'] += min(100, module._remaining_budget())
        return AIAnalysis(explanation='analyzed')

    monkeypatch.setattr(module, '_analyze_testcase', fake_analyze)
    first = _make_schedule_entry(plan='/plans/first')
    second = _make_schedule_entry(plan='/plans/second')
    first_suite = _make_suite([_failed_tc('/test/{}'.format(index)) for index in range(3)])
    second_suite = _make_suite([_failed_tc('/test/{}'.format(index)) for index in range(3)])
    module.serialize_test_schedule_entry_results(first, first_suite)
    module.serialize_test_schedule_entry_results(second, second_suite)
    assert attempted == [('/plans/first', '/test/0'), ('/plans/first', '/test/1'),
                         ('/plans/second', '/test/0'), ('/plans/second', '/test/1')]
    assert clock['now'] == 360
    module.serialize_test_schedule_entry_results(first, _make_suite([_failed_tc('/test/rebuilt')]))
    assert clock['now'] == 360


def test_pending_task_timeout_does_not_disable_remaining_plan_budget(module, monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'sleep',
                        lambda seconds: clock.__setitem__('now', clock['now'] + seconds))
    entry = _make_schedule_entry()
    module._active_plan = entry
    module._plan_deadlines[entry] = 180
    pending = _mock_response(_GOOD_RESPONSE)
    pending.json.return_value['status'] = 'scheduled'
    pending.headers['Retry-After'] = '120'
    timeouts = []

    def fake_wait(label, fn, timeout, tick):
        timeouts.append(timeout)
        return pending if len(timeouts) == 1 else _mock_response(_GOOD_RESPONSE)

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    files = [{'name': 'f', 'content': 'x'}]
    assert module._call_logdetective(files, {}) is None
    assert not module._endpoint_unavailable
    assert module._call_logdetective(files, {}) is not None
    assert timeouts == [pytest.approx(120), pytest.approx(60)]


def test_transient_request_near_plan_deadline_does_not_disable_later_plan(module, monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'monotonic', lambda: clock['now'])
    first = _make_schedule_entry(plan='/plans/first')
    second = _make_schedule_entry(plan='/plans/second')
    module._active_plan = first
    module._plan_deadlines[first] = 15

    def fake_wait(label, fn, timeout, tick):
        clock['now'] += 6
        raise TimeoutError('transient request exhausted retries')

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    files = [{'name': 'f', 'content': 'x'}]
    assert module._call_logdetective(files, {}) is None
    assert not module._endpoint_unavailable
    assert module._remaining_budget() == 9

    module._active_plan = second
    module._plan_deadlines[second] = clock['now'] + 180
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait',
                        MagicMock(return_value=_mock_response(_GOOD_RESPONSE)))
    assert module._call_logdetective(files, {}) is not None


def test_request_retry_stops_before_exceeding_plan_budget(module, monkeypatch):
    """A failed HTTP call near the deadline does not trigger another full retry sleep."""
    module._config['plan-timeout'] = 120
    clock = {'now': 0.0}
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'monotonic', lambda: clock['now'])
    entry = _make_schedule_entry()
    module._active_plan = entry
    module._plan_deadlines[entry] = 120

    class FakeRequests:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, json=None, headers=None, timeout=None):
            clock['now'] = 115
            raise ConnectionError('unavailable')

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests', lambda logger: FakeRequests())

    def fake_wait(label, fn, timeout, tick):
        result = fn()
        assert result.is_ok
        assert tick == 10
        return result.value

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'wait', fake_wait)
    assert module._call_logdetective([{'name': 'f', 'content': 'x'}], {}) is None
    assert clock['now'] == 115


@pytest.mark.parametrize('limit', [0, -1])
def test_sanity_rejects_nonpositive_plan_timeout(module, limit):
    module._config['plan-timeout'] = limit
    with pytest.raises(gluetool.GlueError, match='plan-timeout must be positive'):
        module.sanity()


def test_ready_files_waits_for_primary_artifact(module, monkeypatch):
    """Wait for the main log and accept a plan log served by a GET-only endpoint."""
    clock = {'now': 0.0}
    checks = []
    get_checks = []
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'sleep',
                        lambda seconds: clock.__setitem__('now', clock['now'] + seconds))

    class FakeRequests:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, allow_redirects, timeout, **kwargs):
            assert method in ('HEAD', 'GET')
            if method == 'HEAD':
                checks.append(url)
            if method == 'HEAD' and url.endswith('tmt-run.log'):
                return MagicMock(status_code=405)
            if method == 'GET':
                get_checks.append((url, kwargs['headers'], kwargs['stream']))
                return MagicMock(status_code=206)
            return MagicMock(status_code=200 if clock['now'] >= 5 else 404)

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests', lambda logger: FakeRequests())
    files = [
        {'name': 'tmt-run.log', 'url': 'https://artifacts.example/tmt-run.log'},
        {'name': 'testout.log', 'url': 'https://artifacts.example/testout.log'},
    ]
    assert LogDetective._ready_files(module, files) == files
    assert clock['now'] == 5
    assert checks.count('https://artifacts.example/testout.log') == 2
    assert get_checks == [('https://artifacts.example/tmt-run.log', {'Range': 'bytes=0-0'}, True)]


def test_ready_files_uses_available_logs_after_wait(module, monkeypatch):
    """An unavailable optional URL is never submitted after the bounded wait."""
    clock = {'now': 0.0}
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective.time, 'sleep',
                        lambda seconds: clock.__setitem__('now', clock['now'] + seconds))

    class FakeRequests:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, allow_redirects, timeout, stream):
            assert method == 'HEAD'
            assert stream is True
            return MagicMock(status_code=200 if url.endswith('tmt-run.log') else 404)

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests', lambda logger: FakeRequests())
    files = [
        {'name': 'tmt-run.log', 'url': 'https://artifacts.example/tmt-run.log'},
        {'name': 'testout.log', 'url': 'https://artifacts.example/testout.log'},
        {'name': 'invalid.log', 'url': 'https://artifacts.example/invalid.log?token=x'},
    ]
    assert LogDetective._ready_files(module, files) == files[:1]
    assert clock['now'] == 30


def test_ready_files_streamed_get_200_never_reads_body(module, monkeypatch):
    """Some stores ignore Range and return a full body with 200."""
    responses = []

    class StatusOnlyResponse:
        def __init__(self, status):
            self.status_code = status
            self.closed = False
            responses.append(self)

        @property
        def text(self):
            raise AssertionError('availability check read the log body')

        def close(self):
            self.closed = True

    class FakeRequests:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            assert method in ('HEAD', 'GET')
            assert kwargs['stream'] is True
            if method == 'GET':
                assert kwargs['headers'] == {'Range': 'bytes=0-0'}
            return StatusOnlyResponse(405 if method == 'HEAD' else 200)

    monkeypatch.setattr(gluetool_modules_framework.helpers.logdetective, 'requests',
                        lambda logger: FakeRequests())
    files = [{'name': 'testout.log', 'url': 'https://artifacts.example/testout.log'}]
    assert LogDetective._ready_files(module, files) == files
    assert len(responses) == 2
    assert all(response.closed for response in responses)


# ---------------------------------------------------------------------------
# _analyze_testcase / _analyze_suite_fallback
# ---------------------------------------------------------------------------

def test_analyze_testcase_returns_and_caches(module, tmp_path, monkeypatch):
    """_analyze_testcase analyzes a single testcase and caches on its stable identity."""
    tmt_log = tmp_path / TMT_LOG
    tmt_log.write_text('log', encoding='utf-8')

    se = _make_schedule_entry(
        state=TestScheduleEntryState.OK,
        result=TestScheduleResult.FAILED,
        work_dirpath=str(tmp_path),
        entry_id='ok-plan'
    )
    tc = _failed_tc('/test/foo', logs=[_log('testout.log')])

    analysis = AIAnalysis.from_api_response(_GOOD_RESPONSE)
    monkeypatch.setattr(module, '_call_logdetective', lambda files, metadata: analysis)

    result = module._analyze_testcase(se, tc)
    assert result == analysis
    assert module._analysis_cache[('testcase', se, '/test/foo', None, None)] == analysis


def test_prepare_final_results_publishes_before_analysis(module, monkeypatch):
    publish = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'publish_before_final_report': publish,
        'test_schedule': lambda: [_make_schedule_entry(result=TestScheduleResult.FAILED)]
    })

    module.prepare_final_results()

    publish.assert_called_once_with()


def test_error_state_with_undefined_result_publishes_and_analyzes(module, monkeypatch):
    """A crash before tmt has a result must still publish logs and analyze the suite."""
    entry = _make_schedule_entry(state=TestScheduleEntryState.ERROR,
                                 result=TestScheduleResult.UNDEFINED)
    publish = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'publish_before_final_report': publish,
        'test_schedule': lambda: [entry]
    })
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    analysis = AIAnalysis(explanation='infrastructure failure')
    analyze = MagicMock(return_value=analysis)
    monkeypatch.setattr(module, '_analyze_suite_fallback', analyze)

    module.prepare_final_results()
    suite = _make_suite()
    module.serialize_test_schedule_entry_results(entry, suite)

    publish.assert_called_once_with()
    analyze.assert_called_once_with(entry)
    assert suite.ai_summary is analysis


@pytest.mark.parametrize('result,state', [
    (TestScheduleResult.UNDEFINED, TestScheduleEntryState.OK),
    (TestScheduleResult.PASSED, TestScheduleEntryState.OK),
])
def test_pending_and_passed_entries_remain_ineligible(module, monkeypatch, result, state):
    entry = _make_schedule_entry(result=result, state=state)
    publish = MagicMock()
    analyze = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'publish_before_final_report': publish,
        'test_schedule': lambda: [entry]
    })
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    monkeypatch.setattr(module, '_analyze_suite_fallback', analyze)

    module.prepare_final_results()
    module.serialize_test_schedule_entry_results(entry, _make_suite())

    publish.assert_not_called()
    analyze.assert_not_called()


def test_prepare_final_results_skips_sync_without_failed_tmt_plans(module, monkeypatch):
    publish = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'publish_before_final_report': publish,
        'test_schedule': lambda: [_make_schedule_entry(result=TestScheduleResult.PASSED)]
    })

    module.prepare_final_results()

    publish.assert_not_called()


def test_prepare_final_results_failure_preserves_report_teardown(module, monkeypatch):
    patch_shared(monkeypatch, module, {}, callables={
        'publish_before_final_report': MagicMock(side_effect=RuntimeError('sync failed'))
    })
    warning = MagicMock()
    monkeypatch.setattr(module, 'warn', warning)

    module.prepare_final_results()

    warning.assert_called_once()


def test_analyze_testcase_no_files_returns_none(module, monkeypatch):
    """Missing files do not poison the cache before a later archive pass."""
    se = _make_schedule_entry(
        state=TestScheduleEntryState.OK,
        result=TestScheduleResult.FAILED,
        work_dirpath=None,  # no tmt-run.log
        entry_id='no-files-plan'
    )
    tc = _failed_tc('/test/foo', logs=[])  # and no testcase logs

    # _call_logdetective must not even be reached when there are no files.
    monkeypatch.setattr(module, '_call_logdetective', MagicMock(side_effect=AssertionError('should not call')))

    result = module._analyze_testcase(se, tc)
    assert result is None
    assert ('testcase', se, '/test/foo', None, None) not in module._analysis_cache


def test_analyze_testcase_retries_after_artifact_becomes_available(module, monkeypatch):
    """A missing remote artifact on the first final pass does not prevent later analysis."""
    se = _make_schedule_entry(entry_id='late-artifact')
    tc = _failed_tc(logs=[_log('testout.log')])
    analysis = AIAnalysis(explanation='failure')
    monkeypatch.setattr(module, '_ready_files', MagicMock(side_effect=[[], [{'name': 'testout.log',
                                                                             'url': 'http://artifacts/testout.log'}]]))
    api_call = MagicMock(return_value=analysis)
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    assert module._analyze_testcase(se, tc) is None
    assert module._analyze_testcase(se, tc) == analysis
    api_call.assert_called_once()


def test_analyze_testcase_improves_partial_result_and_caches_complete_result(module, monkeypatch):
    """A later final pass replaces a partial summary using all selected URLs."""
    se = _make_schedule_entry(entry_id='partial-test')
    tc = _failed_tc(logs=[_log('testout.log'), _log('journal.txt')])
    selected = module._build_files_for_testcase(se, tc)
    ready = MagicMock(side_effect=[selected[:1], selected[:1], selected])
    monkeypatch.setattr(module, '_ready_files', ready)
    first = AIAnalysis(explanation='partial')
    complete = AIAnalysis(explanation='complete')
    api_call = MagicMock(side_effect=[first, complete])
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    assert module._analyze_testcase(se, tc) is first
    assert module._analyze_testcase(se, tc) is first
    assert module._analyze_testcase(se, tc) is complete
    assert module._analyze_testcase(se, tc) is complete
    assert ready.call_count == 3
    assert [call.args[0] for call in api_call.call_args_list] == [selected[:1], selected]


def test_analyze_testcase_failed_retry_keeps_partial_summary(module, monkeypatch):
    """A failed request for newly ready logs leaves the earlier summary available."""
    se = _make_schedule_entry(entry_id='failed-retry')
    tc = _failed_tc(logs=[_log('testout.log'), _log('journal.txt')])
    selected = module._build_files_for_testcase(se, tc)
    monkeypatch.setattr(module, '_ready_files', MagicMock(side_effect=[selected[:1], selected]))
    first = AIAnalysis(explanation='partial')
    api_call = MagicMock(side_effect=[first, None])
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    assert module._analyze_testcase(se, tc) is first
    assert module._analyze_testcase(se, tc) is first
    assert module._analysis_cache[('testcase', se, tc.name, None, None)] is first
    assert api_call.call_count == 2


def test_analyze_testcase_retries_changed_url(module, monkeypatch):
    """A new artifact URL can replace a provisional analysis at the same file count."""
    se = _make_schedule_entry(entry_id='changed-url')
    tc = _failed_tc(logs=[_log('testout.log'), _log('journal.txt')])
    monkeypatch.setattr(module, '_ready_files', lambda files: files[:1])
    first = AIAnalysis(explanation='first')
    second = AIAnalysis(explanation='second')
    api_call = MagicMock(side_effect=[first, second])
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    assert module._analyze_testcase(se, tc) is first
    tc.logs[1].href = 'http://artifacts/new-journal.txt'
    assert module._analyze_testcase(se, tc) is second
    assert api_call.call_count == 2


def test_analyze_suite_fallback_returns_and_caches(module, tmp_path, monkeypatch):
    """_analyze_suite_fallback analyzes an errored plan at plan level and caches on the suite key."""
    tmt_log = tmp_path / TMT_LOG
    tmt_log.write_text('log', encoding='utf-8')

    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        result=TestScheduleResult.ERROR,
        work_dirpath=str(tmp_path),
        entry_id='error-plan'
    )

    analysis = AIAnalysis.from_api_response(_GOOD_RESPONSE)
    monkeypatch.setattr(module, '_call_logdetective', lambda files, metadata: analysis)

    result = module._analyze_suite_fallback(se)
    assert result == analysis
    assert module._analysis_cache[('suite', se)] == analysis


def test_analyze_suite_fallback_uses_error_until_log_is_reachable(module, tmp_path, monkeypatch):
    """An unpublished local log does not prevent an interim synthetic analysis."""
    (tmp_path / TMT_LOG).write_text('local log', encoding='utf-8')
    se = _make_schedule_entry(state=TestScheduleEntryState.ERROR, result=TestScheduleResult.ERROR,
                              work_dirpath=str(tmp_path), error_message='Private failure',
                              entry_id='late-plan-log')
    selected = module._build_files_for_suite_fallback(se)
    ready = MagicMock(side_effect=[[], [], selected])
    monkeypatch.setattr(module, '_ready_files', ready)
    interim = AIAnalysis(explanation='interim')
    actual = AIAnalysis(explanation='actual log')
    api_call = MagicMock(side_effect=[interim, actual])
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    assert module._analyze_suite_fallback(se) is interim
    assert module._analyze_suite_fallback(se) is interim
    assert module._analyze_suite_fallback(se) is actual
    assert module._analyze_suite_fallback(se) is actual
    assert api_call.call_args_list[0].args[0] == [
        {'name': 'error.txt', 'content': 'Error message: Private failure\n'}]
    assert api_call.call_args_list[1].args[0] == selected
    assert ready.call_count == 3


def test_analyze_suite_fallback_keeps_real_log_summary_when_urls_disappear(module, tmp_path, monkeypatch):
    """A later empty ready set cannot replace a real-log result with synthetic text."""
    (tmp_path / TMT_LOG).write_text('local log', encoding='utf-8')
    se = _make_schedule_entry(state=TestScheduleEntryState.ERROR, result=TestScheduleResult.ERROR,
                              work_dirpath=str(tmp_path), error_message='failure', entry_id='disappearing-log')
    first_files = module._build_files_for_suite_fallback(se)
    ready = MagicMock(side_effect=[first_files, []])
    monkeypatch.setattr(module, '_ready_files', ready)
    analysis = AIAnalysis(explanation='real log')
    api_call = MagicMock(return_value=analysis)
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    # Keep the first result provisional by selecting a second URL that is unavailable.
    monkeypatch.setattr(module, '_build_files_for_suite_fallback',
                        MagicMock(side_effect=[first_files + [{'name': 'extra.log',
                                                               'url': 'http://artifacts/extra.log'}],
                                               [{'name': 'replacement.log',
                                                 'url': 'http://artifacts/replacement.log'}]]))
    assert module._analyze_suite_fallback(se) is analysis
    assert module._analyze_suite_fallback(se) is analysis
    api_call.assert_called_once()


def test_analyze_suite_fallback_no_files_returns_none(module):
    """Missing suite logs can be retried on the next final pass."""
    se = _make_schedule_entry(
        state=TestScheduleEntryState.ERROR,
        result=TestScheduleResult.ERROR,
        work_dirpath=None,
        entry_id='empty-plan'
    )
    result = module._analyze_suite_fallback(se)
    assert result is None
    assert ('suite', se) not in module._analysis_cache


# ---------------------------------------------------------------------------
# serialize_test_schedule_entry_results — integration path
# ---------------------------------------------------------------------------

def test_serialize_skips_passed_plan(module, monkeypatch):
    """Passed plans are not analyzed."""
    tc_mock = MagicMock(return_value=None)
    suite_mock = MagicMock(return_value=None)
    monkeypatch.setattr(module, '_analyze_testcase', tc_mock)
    monkeypatch.setattr(module, '_analyze_suite_fallback', suite_mock)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    se = _make_schedule_entry(result=TestScheduleResult.PASSED)
    module.serialize_test_schedule_entry_results(se, _make_suite([_failed_tc()]))
    tc_mock.assert_not_called()
    suite_mock.assert_not_called()


def test_serialize_skips_analysis_for_progress_reports(module, monkeypatch):
    """Progress serialization still populates the suite without making analysis calls."""
    underlying = MagicMock()
    analyze = MagicMock()
    monkeypatch.setattr(module, 'overloaded_shared', underlying)
    monkeypatch.setattr(module, '_analyze_testcase', analyze)
    patch_shared(monkeypatch, module, {}, callables={'is_final_results_generation': lambda: False})

    se = _make_schedule_entry(result=TestScheduleResult.FAILED)
    suite = _make_suite([_failed_tc()])
    module.serialize_test_schedule_entry_results(se, suite)

    underlying.assert_called_once_with('serialize_test_schedule_entry_results', se, suite)
    analyze.assert_not_called()


def test_serialize_progress_reattaches_cached_testcase_analysis(module, monkeypatch):
    """A progress tick after final analysis keeps the AI summary in rebuilt results.xml."""
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    patch_shared(monkeypatch, module, {}, callables={'is_final_results_generation': lambda: False})
    schedule_entry = _make_schedule_entry(result=TestScheduleResult.FAILED)
    analysis = AIAnalysis(explanation='cached failure')
    module._analysis_cache[('testcase', schedule_entry, '/test/fail', None, None)] = analysis
    suite = _make_suite([_failed_tc()])
    api_call = MagicMock(side_effect=AssertionError('progress called analysis'))
    monkeypatch.setattr(module, '_call_logdetective', api_call)

    module.serialize_test_schedule_entry_results(schedule_entry, suite)

    assert suite.test_cases[0].ai_summary is analysis
    api_call.assert_not_called()


def test_serialize_progress_reattaches_cached_suite_analysis(module, monkeypatch):
    """An errored plan without failed testcases retains its cached plan summary on progress."""
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    patch_shared(monkeypatch, module, {}, callables={'is_final_results_generation': lambda: False})
    schedule_entry = _make_schedule_entry(result=TestScheduleResult.ERROR)
    analysis = AIAnalysis(explanation='cached plan failure')
    module._analysis_cache[('suite', schedule_entry)] = analysis
    suite = _make_suite([])

    module.serialize_test_schedule_entry_results(schedule_entry, suite)

    assert suite.ai_summary is analysis


def test_serialize_analyzes_all_failing_testcases_in_plan(module, monkeypatch):
    """The shared time budget does not impose an arbitrary per-plan testcase count."""
    analyze = MagicMock(return_value=AIAnalysis(explanation='failure'))
    monkeypatch.setattr(module, '_analyze_testcase', analyze)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    test_cases = [_failed_tc('/test/{}'.format(index)) for index in range(6)]

    module.serialize_test_schedule_entry_results(
        _make_schedule_entry(result=TestScheduleResult.FAILED), _make_suite(test_cases))

    assert analyze.call_count == len(test_cases)
    assert all(test_case.ai_summary is not None for test_case in test_cases)


def test_serialize_skips_non_tmt_runner(module, monkeypatch):
    """Non-tmt schedule entries are not analyzed."""
    tc_mock = MagicMock(return_value=None)
    suite_mock = MagicMock(return_value=None)
    monkeypatch.setattr(module, '_analyze_testcase', tc_mock)
    monkeypatch.setattr(module, '_analyze_suite_fallback', suite_mock)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    se = _make_schedule_entry(result=TestScheduleResult.FAILED, runner_capability='sti')
    module.serialize_test_schedule_entry_results(se, _make_suite([_failed_tc()]))
    tc_mock.assert_not_called()
    suite_mock.assert_not_called()


def test_serialize_sets_ai_summary_on_failing_testcases_only(module, monkeypatch):
    """ai_summary is set on non-passed testcases only, and each is analyzed individually."""
    analysis = AIAnalysis.from_api_response(_GOOD_RESPONSE)
    analyze_mock = MagicMock(return_value=analysis)
    monkeypatch.setattr(module, '_analyze_testcase', analyze_mock)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    tc_fail = _failed_tc('/test/fail')
    tc_pass = _passed_tc('/test/pass')
    suite = _make_suite([tc_fail, tc_pass])

    se = _make_schedule_entry(result=TestScheduleResult.FAILED)
    module.serialize_test_schedule_entry_results(se, suite)

    assert tc_fail.ai_summary == analysis
    assert tc_pass.ai_summary is None
    # Only the failing testcase is analyzed, and it is analyzed on its own.
    analyze_mock.assert_called_once_with(se, tc_fail)


def test_serialize_analyzes_each_failing_testcase_independently(module, monkeypatch):
    """Two failing tests in one plan each get their own (distinct) analysis."""
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    # Return a per-testcase summary so we can prove summaries are not shared across testcases.
    def fake_analyze(schedule_entry, test_case):
        return AIAnalysis(explanation='analysis for {}'.format(test_case.name))

    monkeypatch.setattr(module, '_analyze_testcase', fake_analyze)

    tc_a = _failed_tc('/test/a')
    tc_b = _failed_tc('/test/b')
    suite = _make_suite([tc_a, tc_b])

    se = _make_schedule_entry(result=TestScheduleResult.FAILED)
    module.serialize_test_schedule_entry_results(se, suite)

    assert tc_a.ai_summary.explanation == 'analysis for /test/a'
    assert tc_b.ai_summary.explanation == 'analysis for /test/b'
    assert tc_a.ai_summary != tc_b.ai_summary


def test_serialize_suite_fallback_when_no_failing_testcases(module, monkeypatch):
    """A failed/errored plan with no non-passed testcases falls back to plan-level analysis."""
    analysis = AIAnalysis.from_api_response(_GOOD_RESPONSE)
    tc_mock = MagicMock(return_value=analysis)
    suite_mock = MagicMock(return_value=analysis)
    monkeypatch.setattr(module, '_analyze_testcase', tc_mock)
    monkeypatch.setattr(module, '_analyze_suite_fallback', suite_mock)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    suite = _make_suite([])  # errored plan produced no testcases
    se = _make_schedule_entry(result=TestScheduleResult.ERROR, state=TestScheduleEntryState.ERROR)
    module.serialize_test_schedule_entry_results(se, suite)

    tc_mock.assert_not_called()
    suite_mock.assert_called_once_with(se)
    assert suite.ai_summary == analysis


def test_serialize_suite_fallback_when_all_passed(module, monkeypatch):
    """When every testcase passed, the plan-level fallback is used and no testcase is analyzed."""
    analysis = AIAnalysis.from_api_response(_GOOD_RESPONSE)
    tc_mock = MagicMock(return_value=analysis)
    suite_mock = MagicMock(return_value=analysis)
    monkeypatch.setattr(module, '_analyze_testcase', tc_mock)
    monkeypatch.setattr(module, '_analyze_suite_fallback', suite_mock)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    tc = _passed_tc()
    suite = _make_suite([tc])
    se = _make_schedule_entry(result=TestScheduleResult.FAILED)
    module.serialize_test_schedule_entry_results(se, suite)

    tc_mock.assert_not_called()
    suite_mock.assert_called_once_with(se)
    assert suite.ai_summary == analysis
    assert tc.ai_summary is None


def test_serialize_caches_analysis(module, monkeypatch, tmp_path):
    """Log Detective HTTP call is made at most once per testcase despite two serialize passes."""
    (tmp_path / TMT_LOG).write_text('log', encoding='utf-8')

    analysis = AIAnalysis.from_api_response(_GOOD_RESPONSE)
    http_call_count = {'n': 0}

    def fake_call_logdetective(files, metadata):
        http_call_count['n'] += 1
        return analysis

    monkeypatch.setattr(module, '_call_logdetective', fake_call_logdetective)
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())

    se = _make_schedule_entry(result=TestScheduleResult.FAILED, work_dirpath=str(tmp_path))
    # Fresh suites/testcases each pass, mirroring the report module rebuilding them; same identity.
    suite1 = _make_suite([_failed_tc(logs=[_log('testout.log')])])
    suite2 = _make_suite([_failed_tc(logs=[_log('testout.log')])])

    module.serialize_test_schedule_entry_results(se, suite1)
    module.serialize_test_schedule_entry_results(se, suite2)

    # HTTP call must happen exactly once; the second pass reads from the cache.
    assert http_call_count['n'] == 1
    assert suite1.test_cases[0].ai_summary == analysis
    assert suite2.test_cases[0].ai_summary == analysis


def test_same_id_entries_keep_separate_testcase_analyses_through_progress(module, monkeypatch):
    """A display ID and testcase identity can repeat across distinct schedule entries."""
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    api_call = MagicMock(side_effect=lambda files, metadata: AIAnalysis(explanation=files[0]['url']))
    monkeypatch.setattr(module, '_call_logdetective', api_call)
    entries = [_make_schedule_entry(entry_id='repeated') for _ in range(2)]
    urls = ['http://artifacts/first/testout.log', 'http://artifacts/second/testout.log']

    for entry, url in zip(entries, urls):
        suite = _make_suite([_failed_tc(logs=[_log('testout.log', url)])])
        module.serialize_test_schedule_entry_results(entry, suite)
        assert suite.test_cases[0].ai_summary.explanation == url

    assert api_call.call_count == 2
    patch_shared(monkeypatch, module, {}, callables={'is_final_results_generation': lambda: False})
    for entry, url in zip(entries, urls):
        rebuilt = _make_suite([_failed_tc(logs=[_log('testout.log', url)])])
        module.serialize_test_schedule_entry_results(entry, rebuilt)
        assert rebuilt.test_cases[0].ai_summary.explanation == url
    assert api_call.call_count == 2


def test_same_id_entries_keep_separate_suite_analyses_through_progress(module, monkeypatch, tmp_path):
    """Plan-level cache entries follow the schedule object across report rebuilds."""
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    api_call = MagicMock(side_effect=lambda files, metadata: AIAnalysis(explanation=files[0]['url']))
    monkeypatch.setattr(module, '_call_logdetective', api_call)
    entries = []
    explanations = []
    for index in range(2):
        workdir = tmp_path / str(index)
        workdir.mkdir()
        (workdir / TMT_LOG).write_text('log {}'.format(index), encoding='utf-8')
        entry = _make_schedule_entry(entry_id='repeated', state=TestScheduleEntryState.ERROR,
                                     result=TestScheduleResult.UNDEFINED, work_dirpath=str(workdir))
        suite = _make_suite()
        module.serialize_test_schedule_entry_results(entry, suite)
        entries.append(entry)
        explanations.append(suite.ai_summary.explanation)

    assert explanations[0] != explanations[1]
    assert api_call.call_count == 2
    patch_shared(monkeypatch, module, {}, callables={'is_final_results_generation': lambda: False})
    for entry, explanation in zip(entries, explanations):
        rebuilt = _make_suite()
        module.serialize_test_schedule_entry_results(entry, rebuilt)
        assert rebuilt.ai_summary.explanation == explanation
    assert api_call.call_count == 2


def test_serialize_analysis_failure_no_exception(module, monkeypatch):
    """An exception in analysis is caught and does not propagate to the pipeline."""
    monkeypatch.setattr(module, 'overloaded_shared', MagicMock())
    monkeypatch.setattr(module, '_analyze_testcase', MagicMock(side_effect=RuntimeError('boom')))

    se = _make_schedule_entry(result=TestScheduleResult.FAILED)
    suite = _make_suite([_failed_tc()])
    module.serialize_test_schedule_entry_results(se, suite)  # must not raise

    assert suite.test_cases[0].ai_summary is None


# ---------------------------------------------------------------------------
# Data model round-trip — ai_summary is present in XML output
# ---------------------------------------------------------------------------

def test_ai_summary_serialized_in_xunit(module):
    """ai_summary on a TestCase appears as a structured child element in the TF xunit output."""
    from gluetool_modules_framework.libs.results import Results, TestSuite as RS, TestCase as RC

    tc = RC(
        name='/test/fail',
        result='failed',
        ai_summary=AIAnalysis(
            explanation='oops',
            solution='fix it',
            snippets=[
                AIAnalysisSnippet(text='ImportError', line_number=42, source_file='test.py'),
                AIAnalysisSnippet(text='ImportError2', line_number=43, source_file='test.py')
            ],
        ),
    )
    suite = RS(name='/plans/basic', result='failed', test_cases=[tc])
    results = Results(overall_result='failed', test_suites=[suite])

    xml = results.xunit_testing_farm.to_xml_string(pretty_print=True)
    assert '<ai-summary' in xml
    assert '<explanation>oops</explanation>' in xml
    assert '<solution>fix it</solution>' in xml
    assert '<snippets>' in xml
    assert '<snippet line-number="42" source-file="test.py">ImportError</snippet>' in xml
    assert '<snippet line-number="43" source-file="test.py">ImportError2</snippet>' in xml


def test_api_text_is_xml_safe_in_serialized_results(module):
    from gluetool_modules_framework.libs.results import Results, TestSuite as RS, TestCase as RC

    analysis = AIAnalysis.from_api_response({
        'explanation': 'bad\x00\tline\n\ud800done',
        'solution': 'fix\x01\rnow',
        'snippets': [{'text': 'trace\x08\nend', 'source_file': 'a\ufffe\U00010000.log'}],
    })
    assert analysis.explanation == 'bad\tline\ndone'
    assert analysis.solution == 'fix\rnow'
    assert analysis.snippets[0].text == 'trace\nend'
    assert analysis.snippets[0].source_file == 'a\U00010000.log'

    results = Results(overall_result='failed', test_suites=[
        RS(name='plan', result='failed', test_cases=[RC(name='test', result='failed', ai_summary=analysis)])
    ])
    xml = results.xunit_testing_farm.to_xml_string(pretty_print=True)
    ElementTree.fromstring(xml)
    assert 'bad\tline\n' in xml


def test_ai_summary_serialized_in_xunit_with_no_snippets(module):
    """ai_summary on a TestCase appears as a structured child element in the TF xunit output
    with no `<snippets>` if none were supplied."""
    from gluetool_modules_framework.libs.results import Results, TestSuite as RS, TestCase as RC

    tc = RC(
        name='/test/fail',
        result='failed',
        ai_summary=AIAnalysis(
            explanation='oops',
            solution='fix it',
        ),
    )
    suite = RS(name='/plans/basic', result='failed', test_cases=[tc])
    results = Results(overall_result='failed', test_suites=[suite])

    xml = results.xunit_testing_farm.to_xml_string(pretty_print=True)
    assert '<ai-summary' in xml
    assert '<explanation>oops</explanation>' in xml
    assert '<solution>fix it</solution>' in xml
    assert '<snippets>' not in xml


def test_ai_summary_absent_when_none(module):
    """ai_summary is omitted from XML when not set."""
    from gluetool_modules_framework.libs.results import Results, TestSuite as RS, TestCase as RC

    tc = RC(name='/test/pass', result='passed')
    suite = RS(name='/plans/basic', result='passed', test_cases=[tc])
    results = Results(overall_result='passed', test_suites=[suite])

    xml = results.xunit_testing_farm.to_xml_string(pretty_print=True)
    assert '<ai-summary>' not in xml


def test_suite_ai_summary_serialized(module):
    """ai_summary on a TestSuite appears in the TF xunit output."""
    from gluetool_modules_framework.libs.results import Results, TestSuite as RS

    suite = RS(name='/plans/infra-fail', result='error', ai_summary=AIAnalysis(explanation='provision fail'))
    results = Results(overall_result='error', test_suites=[suite])

    xml = results.xunit_testing_farm.to_xml_string(pretty_print=True)
    assert '<ai-summary' in xml
    assert '<explanation>provision fail</explanation>' in xml
