# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import glob
import time
import pytest
import logging
import shutil
import tempfile
from pathlib import Path
from mock import MagicMock, call

import gluetool
import gluetool_modules_framework.helpers.archive
from gluetool_modules_framework.helpers.archive import Archive

from . import create_module, check_loadable, patch_shared

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'archive')


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(Archive)[1]

    module._config['artifacts-host'] = 'https://artifacts.example.com'
    module._config['artifacts-rsync-host'] = 'artifacts-rsync.example.com'
    module._config['artifacts-root'] = '/artifacts-root'
    module._config['artifacts-local-root'] = '/artifacts-root'
    module._config['source-destination-map'] = '{}/source-destination-map.yaml'.format(ASSETS_DIR)
    module._config['archive-mode'] = 'ssh'
    module._config['rsync-options'] = '--rsync-option'
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 5
    module._config['verify-tick'] = 1
    module._config['verify-timeout'] = 1
    module._config['rsync-timeout'] = 10
    module._config['aws-access-key-id'] = 'aws-access-key-id'
    module._config['aws-secret-access-key'] = 'aws-secret'
    module._config['aws-region'] = 'aws-region'
    module._config['aws-s3-bucket'] = 'aws-s3-bucket'
    module._config['aws-options'] = '--aws-option'
    module._config['parallel-archiving-finish-tick'] = 1
    module._config['parallel-archiving-finish-timeout'] = 5

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(id='request-id'),
        'artifacts_location': lambda path: 'https://artifacts.example.com/{}'.format(path)
    })

    os.environ['SOURCE_DESTINATION_MAP'] = '/env-archive-source::env-dest:666:destroy#/env-archive-source2::::execute'

    return module


def _mock_glob(path, recursive=False):
    if 'archive-excludes' in path:
        return ['/archive-excludes/exclude-1', '/archive-excludes/exclude-2']
    if '*' in path:
        return ['/dir-archive-source/1', '/dir-archive-source/2', '/dir-archive-source/3']
    if 'archive-source' in path:
        return [path]
    if 'batch-source' in path or 'batch-relative' in path:
        return [path]

    return glob.glob(path, recursive=recursive)


def _rsync_calls(mock_command_init):
    """
    All rsync argv lists passed to ``Command``, in call order.

    ``assert_has_calls`` cannot prove the absence of a call, and the whole point of batching is that
    N invocations became one, so the batched tests assert on exact lists instead.
    """

    return [
        c.args[0] for c in mock_command_init.call_args_list
        if c.args and c.args[0][:1] == ['rsync']
    ]


def _batched_rsync_calls(mock_command_init):
    return [
        argv for argv in _rsync_calls(mock_command_init)
        if any(arg.startswith('--files-from=') for arg in argv)
    ]


def _mkdir_calls(mock_command_init):
    return [
        c.args[0] for c in mock_command_init.call_args_list
        if c.args and c.args[0][:1] == ['ssh'] and 'mkdir' in c.args[0]
    ]


def _patch_transfer(monkeypatch, module, mkdtemp):
    """
    The monkeypatching every transfer test shares. Returns the ``Command.__init__`` mock, which is
    what the argv assertions are made against.
    """

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')
    mock_requests = MagicMock()
    mock_requests.return_value.__enter__.return_value.head.return_value.status_code = 200

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(shutil, 'copytree', MagicMock())
    monkeypatch.setattr(shutil, 'copy2', MagicMock())
    monkeypatch.setattr(shutil, 'rmtree', MagicMock())
    monkeypatch.setattr(tempfile, 'mkdtemp', mkdtemp)
    monkeypatch.setattr(os.path, 'exists', lambda _: True)

    def _isdir(path):
        if path in ['/dir-archive-source', 'dir-archive-source']:
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)
    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    return mock_command_init


@pytest.fixture(name='batched_module')
def fixture_batched_module(monkeypatch, module):
    module._config['source-destination-map'] = '{}/source-destination-map-batched.yaml'.format(ASSETS_DIR)
    module._config['enable-batched-archiving'] = 'yes'
    module._config['enable-parallel-archiving'] = False

    # The environment variable map would add entries to the very stages under test.
    monkeypatch.delenv('SOURCE_DESTINATION_MAP', raising=False)

    return module


def test_sanity(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/archive.py', 'Archive')

    module._config['archive-mode'] = 'invalid'

    with pytest.raises(gluetool.GlueError, match='invalid archive modes: invalid'):
        module.sanity()

    module._config['archive-mode'] = 'daemon'
    module._config['artifacts-rsync-host'] = None
    with pytest.raises(gluetool.GlueError, match='rsync daemon host must be specified when using rsync daemon mode'):
        module.sanity()

    module._config['archive-mode'] = 'ssh'
    module._config['artifacts-host'] = None
    with pytest.raises(gluetool.GlueError, match='artifacts host must be specified when using ssh mode'):
        module.sanity()

    module._config['archive-mode'] = 'local'
    module._config['artifacts-local-root'] = None
    with pytest.raises(gluetool.GlueError, match='artifacts local root must be specified when using local mode'):
        module.sanity()


@pytest.mark.parametrize('value, expected', [
    ('ssh', ['ssh']),
    ('daemon,s3', ['daemon', 's3']),
    ('daemon, s3', ['daemon', 's3']),
    (['ssh', 's3'], ['ssh', 's3']),
    (['daemon,ssh', 's3'], ['daemon', 'ssh', 's3']),
    ('ssh,ssh', ['ssh']),
    (['daemon,s3', 'daemon', 's3,ssh'], ['daemon', 's3', 'ssh']),
])
def test_archive_modes(module, value, expected):
    module._config['archive-mode'] = value

    assert module.archive_modes == expected
    module.sanity()


@pytest.mark.parametrize('args, expected', [
    (['--archive-mode', 'ssh'], ['ssh']),
    (['--archive-mode', 'daemon,s3'], ['daemon', 's3']),
    (['--archive-mode', 'ssh', '--archive-mode', 's3'], ['ssh', 's3']),
    (['--archive-mode', 'daemon,ssh', '--archive-mode', 's3'], ['daemon', 'ssh', 's3']),
    (['--archive-mode', 'ssh,ssh', '--archive-mode', 'ssh'], ['ssh']),
])
def test_archive_modes_cli(module, args, expected):
    module.parse_args(args)

    assert module.archive_modes == expected


@pytest.mark.parametrize('archive_modes, invalid', [
    ('ssh,invalid', 'invalid'),
    (['daemon', 'invalid'], 'invalid'),
    ('ssh,invalid,unknown', 'invalid, unknown'),
    ([], r'\(none\)'),
    ('', r'\(none\)'),
])
def test_sanity_invalid_modes(module, archive_modes, invalid):
    module._config['archive-mode'] = archive_modes

    with pytest.raises(gluetool.GlueError, match='invalid archive modes: {}'.format(invalid)):
        module.sanity()


@pytest.mark.parametrize('archive_modes, missing_option, message', [
    (['s3', 'daemon'], 'artifacts-rsync-host', 'rsync daemon host'),
    (['s3', 'ssh'], 'artifacts-host', 'artifacts host'),
    (['s3', 'local'], 'artifacts-local-root', 'artifacts local root'),
    (['daemon', 's3'], 'aws-region', 'aws-region option'),
    (['daemon', 's3'], 'aws-s3-bucket', 'aws-s3-bucket option'),
    (['daemon', 's3'], 'aws-access-key-id', 'aws-access-key-id option'),
    (['daemon', 's3'], 'aws-secret-access-key', 'aws-secret-access-key option'),
])
def test_sanity_multiple_modes(module, archive_modes, missing_option, message):
    module._config['archive-mode'] = archive_modes
    module._config[missing_option] = None

    with pytest.raises(gluetool.GlueError, match=message):
        module.sanity()


@pytest.mark.parametrize('stage', ['execute', 'progress', 'destroy'])
@pytest.mark.parametrize('archive_modes', [
    ['daemon', 's3'], ['s3', 'ssh'], ['local', 's3'], ['ssh', 'local'], ['local']
])
def test_archive_stage_multiple_modes(monkeypatch, module, stage, archive_modes):
    module._config['archive-mode'] = archive_modes
    module._request_id = 'request-id'
    transfers = MagicMock()
    monkeypatch.setattr(module, 'prepare_source_copy', lambda source: ('/snapshot/archive-source', source))
    monkeypatch.setattr(module, 'delete_source_copy', MagicMock())
    transfers.verify.return_value.status_code = 200
    mock_requests = MagicMock()
    mock_requests.return_value.__enter__.return_value.head = transfers.verify

    monkeypatch.setattr(module, 'run_rsync', transfers.rsync)
    monkeypatch.setattr(module, 'run_aws', transfers.aws)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)
    monkeypatch.setattr(module, 'source_destination_map', lambda: {
        stage: [{'source': '/archive-source', 'destination': 'dest', 'permissions': '666', 'verify': 'true'}]
    })

    module.archive_stage(stage)

    source = '/archive-source' if stage == 'destroy' else '/snapshot/archive-source'
    original_source = None if stage == 'destroy' else '/archive-source'
    expected = []
    for archive_mode in archive_modes:
        if archive_mode == 's3':
            expected.append(call.aws(source, 'dest', options=None, original_source=original_source))
        else:
            expected.append(call.rsync(
                source, 'dest', archive_mode, options=['--chmod=666'], original_source=original_source
            ))

    if archive_modes != ['local']:
        expected.append(call.verify('https://artifacts.example.com/archive-source', allow_redirects=True))

    assert transfers.mock_calls == expected


@pytest.mark.parametrize('archive_modes', [
    ['daemon', 's3'], ['s3', 'daemon'], ['ssh', 's3'], ['s3', 'ssh'], ['local', 's3'], ['s3', 'local']
])
@pytest.mark.parametrize('failing_mode', ['rsync', 's3'])
def test_archive_stage_mode_failure(monkeypatch, module, log, archive_modes, failing_mode):
    module._config['archive-mode'] = archive_modes
    module._request_id = 'request-id'
    rsync_mode = next(mode for mode in archive_modes if mode != 's3')
    mock_rsync = MagicMock(side_effect=gluetool.GlueError('upload failed') if failing_mode == 'rsync' else None)
    mock_aws = MagicMock(side_effect=gluetool.GlueError('upload failed') if failing_mode == 's3' else None)
    mock_requests = MagicMock()
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)

    monkeypatch.setattr(module, 'run_rsync', mock_rsync)
    monkeypatch.setattr(module, 'run_aws', mock_aws)
    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)
    monkeypatch.setattr(module, 'source_destination_map', lambda: {
        'destroy': [{'source': '/archive-source', 'destination': 'dest', 'verify': 'true'}]
    })

    module.archive_stage('destroy')

    mock_rsync.assert_called_once_with('/archive-source', 'dest', rsync_mode, options=None, original_source=None)
    mock_aws.assert_called_once_with('/archive-source', 'dest', options=None, original_source=None)
    mock_requests.assert_not_called()
    errors = [record.message for record in log.records if record.levelno == logging.ERROR]
    assert errors == ['Failed to sync /archive-source using {}: upload failed'.format(
        rsync_mode if failing_mode == 'rsync' else 's3'
    )]


@pytest.mark.parametrize('stage', ['execute', 'progress', 'destroy'])
@pytest.mark.parametrize('archive_modes', [['daemon', 's3'], ['s3', 'ssh'], ['local', 's3']])
@pytest.mark.parametrize('failing_mode', [None, 'rsync', 's3', 'all'])
def test_archive_stage_prepares_source_once(monkeypatch, module, tmp_path, stage, archive_modes, failing_mode):
    module._config['archive-mode'] = archive_modes
    module._request_id = 'request-id'
    source = tmp_path / 'progress.log'
    source.write_text('secret')
    snapshots = []
    mock_command = MagicMock()
    copy_source = MagicMock(wraps=module.prepare_source_copy)
    delete_source = MagicMock(wraps=module.delete_source_copy)

    def hide_secrets(search_path):
        path = Path(search_path)
        path.write_text(path.read_text().replace('secret', 'hidden'))

    mock_hide = MagicMock(side_effect=hide_secrets)

    def upload(cmd, **kwargs):
        if cmd[0] not in ['rsync', 'aws']:
            return
        path = Path(cmd[-2])
        snapshots.append((path, path.read_text()))
        # A live log changes after the first upload, but the snapshot must stay unchanged.
        if stage != 'destroy':
            source.write_text('secret appended')
        if failing_mode == 'all' or (failing_mode == 's3' and cmd[0] == 'aws') or (
            failing_mode == 'rsync' and cmd[0] == 'rsync'
        ):
            raise gluetool.GlueError('upload failed')

    mock_command.side_effect = upload
    patch_shared(monkeypatch, module, {}, callables={'hide_secrets': mock_hide})
    monkeypatch.setattr(module, 'prepare_source_copy', copy_source)
    monkeypatch.setattr(module, 'delete_source_copy', delete_source)
    monkeypatch.setattr(module, 'source_destination_map', lambda: {
        stage: [{'source': str(source), 'destination': 'dest'}]
    })
    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command)
    monkeypatch.setattr(gluetool.utils.Command, 'run', MagicMock())

    module.archive_stage(stage)

    assert len(snapshots) == 2
    assert snapshots[0] == snapshots[1]
    assert snapshots[0][1] == 'hidden'
    mock_hide.assert_called_once_with(search_path=str(snapshots[0][0]))
    if stage == 'destroy':
        copy_source.assert_not_called()
        delete_source.assert_not_called()
    else:
        copy_source.assert_called_once_with(str(source))
        delete_source.assert_called_once_with(str(snapshots[0][0]))
        assert not snapshots[0][0].parent.exists()
        assert source.read_text() == 'secret appended'


@pytest.mark.parametrize('stage', ['execute', 'progress'])
def test_archive_stage_redaction_failure(monkeypatch, module, tmp_path, stage):
    module._config['archive-mode'] = ['daemon', 's3']
    module._request_id = 'request-id'
    source = tmp_path / 'progress.log'
    source.write_text('secret')
    copy_source = MagicMock(wraps=module.prepare_source_copy)
    delete_source = MagicMock(wraps=module.delete_source_copy)
    mock_rsync = MagicMock()
    mock_aws = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'hide_secrets': MagicMock(side_effect=gluetool.GlueError('redaction failed'))
    })
    monkeypatch.setattr(module, 'prepare_source_copy', copy_source)
    monkeypatch.setattr(module, 'delete_source_copy', delete_source)
    monkeypatch.setattr(module, 'run_rsync', mock_rsync)
    monkeypatch.setattr(module, 'run_aws', mock_aws)
    monkeypatch.setattr(module, 'source_destination_map', lambda: {stage: [{'source': str(source)}]})

    module.archive_stage(stage)

    copy_source.assert_called_once_with(str(source))
    delete_source.assert_called_once()
    assert not Path(delete_source.call_args[0][0]).parent.exists()
    assert source.read_text() == 'secret'
    mock_rsync.assert_not_called()
    mock_aws.assert_not_called()


@pytest.mark.parametrize('archive_modes', [['ssh', 'local', 'daemon'], ['local', 'daemon', 'ssh']])
def test_create_directories_multiple_modes(monkeypatch, module, archive_modes):
    module._config['archive-mode'] = archive_modes
    mock_command_init = MagicMock(return_value=None)
    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', MagicMock())
    monkeypatch.setattr(module, 'archive_stage', MagicMock())

    module.execute()

    mock_command_init.assert_has_calls([
        call(['ssh', 'https://artifacts.example.com', 'mkdir', '-p', '/artifacts-root/request-id'],
             logger=module.logger),
        call(['mkdir', '-p', '/artifacts-root/request-id'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '--mkpath', '/dev/null',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),
    ], any_order=True)

    mock_command_init.reset_mock()
    module.create_archive_directory_ssh()
    module.create_archive_directory_local()
    module.create_archive_directory_rsync()
    mock_command_init.assert_not_called()


@pytest.mark.parametrize('archive_modes', ['ssh', 'ssh,s3', 's3,ssh'])
def test_execute_destroy_ssh(monkeypatch, module, archive_modes):
    module._config['archive-mode'] = archive_modes
    module._config['enable-parallel-archiving'] = False

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')
    mock_shutil_copytree = MagicMock()
    mock_shutil_copy2 = MagicMock()
    mock_shutil_rmtree = MagicMock()
    mock_requests = MagicMock()
    mock_requests_head = mock_requests.return_value.__enter__.return_value.head
    mock_requests_head.return_value.status_code = 200

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(shutil, 'copytree', mock_shutil_copytree)
    monkeypatch.setattr(shutil, 'copy2', mock_shutil_copy2)
    monkeypatch.setattr(shutil, 'rmtree', mock_shutil_rmtree)
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda: '/tmp/dir')

    monkeypatch.setattr(os.path, 'exists', lambda _: True)

    def _isdir(path):
        if path in ['/dir-archive-source', 'dir-archive-source']:
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    # run execute to test directory creation
    module.execute()

    module.destroy()

    calls = [
        call(['ssh', 'https://artifacts.example.com', 'mkdir', '-p',
              '/artifacts-root/request-id'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/tmp/dir/archive-source-execute',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-source-execute'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--recursive', '/dir-archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/'], logger=module.logger),

        call(['ssh', 'https://artifacts.example.com', 'mkdir', '-p',
              '/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/1',
              'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/2',
              'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/3',
              'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/env-archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/env-dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/tmp/dir/env-archive-source2',
              'https://artifacts.example.com:/artifacts-root/request-id/env-archive-source2'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-excludes/exclude-2',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-excludes'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)
    mock_requests_head.assert_called_once_with(
        'https://artifacts.example.com/archive-source-execute',
        allow_redirects=True
    )


@pytest.mark.parametrize('archive_modes', ['daemon', 'daemon,s3', 's3,daemon'])
def test_destroy_daemon(monkeypatch, module, archive_modes):
    module._config['archive-mode'] = archive_modes

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')
    mock_shutil_copytree = MagicMock()
    mock_shutil_copy2 = MagicMock()
    mock_shutil_rmtree = MagicMock()
    mock_requests = MagicMock()
    mock_requests_head = mock_requests.return_value.__enter__.return_value.head
    mock_requests_head.return_value.status_code = 200

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(shutil, 'copytree', mock_shutil_copytree)
    monkeypatch.setattr(shutil, 'copy2', mock_shutil_copy2)
    monkeypatch.setattr(shutil, 'rmtree', mock_shutil_rmtree)
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda: '/tmp/dir')

    monkeypatch.setattr(os.path, 'exists', lambda _: True)

    def _isdir(path):
        if path in ['/dir-archive-source', 'dir-archive-source']:
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    module.execute()

    module.destroy()

    calls = [
        call(['rsync', '--rsync-option', '--timeout=10', '--mkpath', '/dev/null',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'rsync://artifacts-rsync.example.com/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/archive-source',
              'rsync://artifacts-rsync.example.com/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--recursive', '/dir-archive-source',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--mkpath', '/dev/null',
              'rsync://artifacts-rsync.example.com/request-id/dir-archive-source/'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/1',
              'rsync://artifacts-rsync.example.com/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/2',
              'rsync://artifacts-rsync.example.com/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/3',
              'rsync://artifacts-rsync.example.com/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/env-archive-source',
              'rsync://artifacts-rsync.example.com/request-id/env-dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-excludes/exclude-2',
              'rsync://artifacts-rsync.example.com/request-id/archive-excludes'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)


@pytest.mark.parametrize('archive_modes', ['local', 'local,s3', 's3,local'])
def test_execute_destroy_local(monkeypatch, module, archive_modes):
    module._config['enable-parallel-archiving'] = False
    module._config['archive-mode'] = archive_modes

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')
    mock_shutil_copytree = MagicMock()
    mock_shutil_copy2 = MagicMock()
    mock_shutil_rmtree = MagicMock()
    mock_requests = MagicMock()
    mock_requests_head = mock_requests.return_value.__enter__.return_value.head
    mock_requests_head.return_value.status_code = 200

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(shutil, 'copytree', mock_shutil_copytree)
    monkeypatch.setattr(shutil, 'copy2', mock_shutil_copy2)
    monkeypatch.setattr(shutil, 'rmtree', mock_shutil_rmtree)
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda: '/tmp/dir')

    monkeypatch.setattr(os.path, 'exists', lambda _: True)

    def _isdir(path):
        if path in ['/dir-archive-source', 'dir-archive-source']:
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    # run execute to test directory creation
    module.execute()

    module.destroy()

    calls = [
        call(['mkdir', '-p', '/artifacts-root/request-id'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              '/artifacts-root/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              '/artifacts-root/request-id/'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/archive-source',
              '/artifacts-root/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--recursive', '/dir-archive-source',
              '/artifacts-root/request-id/'], logger=module.logger),

        call(['mkdir', '-p', '/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/1',
              '/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/2',
              '/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/3',
              '/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/env-archive-source',
              '/artifacts-root/request-id/env-dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-excludes/exclude-2',
              '/artifacts-root/request-id/archive-excludes'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)


@pytest.mark.parametrize('archive_modes', ['s3', 'daemon,s3', 'ssh,s3', 'local,s3'])
def test_execute_destroy_s3(monkeypatch, module, archive_modes):
    module._config['enable-parallel-archiving'] = False
    module._config['archive-mode'] = archive_modes

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')
    mock_shutil_copytree = MagicMock()
    mock_shutil_copy2 = MagicMock()
    mock_shutil_rmtree = MagicMock()
    mock_requests = MagicMock()
    mock_requests_head = mock_requests.return_value.__enter__.return_value.head
    mock_requests_head.return_value.status_code = 200

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(shutil, 'copytree', mock_shutil_copytree)
    monkeypatch.setattr(shutil, 'copy2', mock_shutil_copy2)
    monkeypatch.setattr(shutil, 'rmtree', mock_shutil_rmtree)
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda: '/tmp/dir')

    monkeypatch.setattr(os.path, 'exists', lambda _: True)

    def _isdir(path):
        if path in ['/dir-archive-source', 'dir-archive-source']:
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    # run execute to test directory creation
    module.execute()

    module.destroy()

    calls = [
        call(['aws', 's3', 'cp', '--aws-option', '/archive-source',
              's3://aws-s3-bucket/artifacts-root/request-id/dest'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/archive-source',
              's3://aws-s3-bucket/artifacts-root/request-id/archive-source'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/archive-source',
              's3://aws-s3-bucket/artifacts-root/request-id/dest'], logger=module.logger),

        call(['aws', 's3', 'sync', '--aws-option', '/dir-archive-source',
              's3://aws-s3-bucket/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/dir-archive-source/1',
              's3://aws-s3-bucket/artifacts-root/request-id/dir-archive-source/1'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/dir-archive-source/2',
              's3://aws-s3-bucket/artifacts-root/request-id/dir-archive-source/2'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/dir-archive-source/3',
              's3://aws-s3-bucket/artifacts-root/request-id/dir-archive-source/3'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/env-archive-source',
              's3://aws-s3-bucket/artifacts-root/request-id/env-dest'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/tmp/dir/archive-source-execute',
              's3://aws-s3-bucket/artifacts-root/request-id/archive-source-execute'], logger=module.logger),

        call(['aws', 's3', 'cp', '--aws-option', '/tmp/dir/env-archive-source2',
              's3://aws-s3-bucket/artifacts-root/request-id/env-archive-source2'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)


@pytest.mark.parametrize('archive_modes', ['ssh', 'ssh,s3'])
def test_parallel_archiving(monkeypatch, module, log, archive_modes):
    module._config['archive-mode'] = archive_modes
    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')
    mock_shutil_copytree = MagicMock()
    mock_shutil_copy2 = MagicMock()
    mock_shutil_rmtree = MagicMock()
    mock_requests = MagicMock()
    mock_requests_head = mock_requests.return_value.__enter__.return_value.head
    mock_requests_head.return_value.status_code = 200

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    monkeypatch.setattr(gluetool.utils, 'requests', mock_requests)
    monkeypatch.setattr(shutil, 'copytree', mock_shutil_copytree)
    monkeypatch.setattr(shutil, 'copy2', mock_shutil_copy2)
    monkeypatch.setattr(shutil, 'rmtree', mock_shutil_rmtree)
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda: '/tmp/dir')

    monkeypatch.setattr(os.path, 'exists', lambda _: True)

    def _isdir(path):
        if path in [
            '/dir-archive-source',
            '/archive-source-another-progress',
            '/tmp/dir/archive-source-another-progress'
        ]:
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    module._config['enable-parallel-archiving'] = True
    module._config['parallel-archiving-tick'] = 0.1

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message == 'Starting parallel archiving, run every 0.1 seconds'

    # make sure the timer runs
    time.sleep(0.5)

    module.destroy()

    assert log.match(levelno=logging.INFO, message='Stopping parallel archiving')
    assert log.match(
        levelno=logging.DEBUG,
        message='syncing /tmp/dir/archive-source-progress to '
                'https://artifacts.example.com:/artifacts-root/request-id/archive-source-progress'
    )
    assert log.match(
        levelno=logging.DEBUG,
        message='syncing /tmp/dir/archive-source-another-progress to '
                'https://artifacts.example.com:/artifacts-root/request-id/archive-source-another-progress'
    )

    if 's3' in module.archive_modes:
        assert log.match(
            levelno=logging.DEBUG,
            message='syncing /tmp/dir/archive-source-progress to '
                    's3://aws-s3-bucket/artifacts-root/request-id/archive-source-progress'
        )

    mock_shutil_copytree.assert_called_with(
        '/archive-source-another-progress', '/tmp/dir/archive-source-another-progress',
        symlinks=True, ignore_dangling_symlinks=True, dirs_exist_ok=True
    )
    mock_shutil_rmtree.assert_called_with('/tmp/dir')

    mock_shutil_copy2.assert_called_with(
        '/archive-source-progress', '/tmp/dir/archive-source-progress', follow_symlinks=False
    )


# The four batchable entries of the batched asset, sorted. `archive-excludes/exclude-2` is in here
# and `exclude-1` is not, which is what proves the per entry exclude regexes are applied before
# batching rather than after it.
BATCH_LIST = (
    b'archive-excludes/exclude-2\x00'
    b'batch-relative/nested-3\x00'
    b'batch-source-1\x00'
    b'batch-source-2\x00'
)


def test_batched_archiving_ssh(monkeypatch, batched_module, tmp_path):
    module = batched_module

    mock_hide_secrets = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(id='request-id'),
        'artifacts_location': lambda path: 'https://artifacts.example.com/{}'.format(path),
        'hide_secrets': mock_hide_secrets,
    })

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    # Exactly one batched invocation, carrying the three batchable entries.
    batched = _batched_rsync_calls(mock_command_init)
    assert len(batched) == 1
    assert batched[0] == [
        'rsync', '--rsync-option', '--timeout=10',
        '--files-from={}/files-from.txt'.format(tmp_path), '--from0', '--relative',
        '--ignore-missing-args', '--no-implied-dirs',
        '{}/tree/'.format(tmp_path),
        'https://artifacts.example.com:/artifacts-root/request-id/',
    ]

    # The list contents are invisible to the argv assertion, assert them directly. Sorted, so the
    # non-alphabetical order in the asset is normalised.
    assert (tmp_path / 'files-from.txt').read_bytes() == BATCH_LIST

    # The non-batchable entries keep their per-file invocations, unchanged.
    per_file = [argv for argv in _rsync_calls(mock_command_init) if argv not in batched]
    assert per_file == [
        ['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '{}/batch-source-permissions'.format(tmp_path),
         'https://artifacts.example.com:/artifacts-root/request-id/batch-source-permissions'],
        ['rsync', '--rsync-option', '--timeout=10', '--recursive', '{}/dir-archive-source'.format(tmp_path),
         'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'],
        ['rsync', '--rsync-option', '--timeout=10', '{}/batch-source-verify'.format(tmp_path),
         'https://artifacts.example.com:/artifacts-root/request-id/batch-source-verify'],
    ]

    # `--no-implied-dirs` lets rsync create the parents, so the batch adds no `mkdir -p` round trips
    # beyond the request root.
    assert _mkdir_calls(mock_command_init) == [
        ['ssh', 'https://artifacts.example.com', 'mkdir', '-p', '/artifacts-root/request-id'],
    ]

    # One pass over the staged tree rather than one per file.
    mock_hide_secrets.assert_any_call(search_path='{}/tree'.format(tmp_path))
    assert mock_hide_secrets.call_args_list.count(
        call(search_path='{}/tree'.format(tmp_path))
    ) == 1


@pytest.mark.parametrize('mode, destination', [
    ('daemon', 'rsync://artifacts-rsync.example.com/request-id/'),
    ('local', '/artifacts-root/request-id/'),
])
def test_batched_archiving_destination(monkeypatch, batched_module, tmp_path, mode, destination):
    module = batched_module
    module._config['archive-mode'] = mode

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    batched = _batched_rsync_calls(mock_command_init)
    assert len(batched) == 1
    assert batched[0][-1] == destination
    assert (tmp_path / 'files-from.txt').read_bytes() == BATCH_LIST


def test_batched_archiving_skips_s3(monkeypatch, batched_module, tmp_path):
    module = batched_module
    module._config['archive-mode'] = 's3'

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    # S3 does not go through rsync at all, batching must not touch it.
    assert _batched_rsync_calls(mock_command_init) == []
    assert not (tmp_path / 'files-from.txt').exists()
    assert [c.args[0][:2] for c in mock_command_init.call_args_list if c.args] == [['aws', 's3']] * 7


def test_batched_archiving_single_file_not_batched(monkeypatch, module, tmp_path):
    # The default asset yields no multi-file batch, so nothing may be batched even with the flag on.
    module._config['enable-batched-archiving'] = 'yes'
    module._config['enable-parallel-archiving'] = False

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    assert _batched_rsync_calls(mock_command_init) == []
    assert not (tmp_path / 'files-from.txt').exists()


def test_batched_archiving_fallback(monkeypatch, batched_module, tmp_path, log):
    module = batched_module

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    # Fail only the batched invocation, so the fallback has something to fall back to.
    def _mock_run(*args, **kwargs):
        argv = mock_command_init.call_args.args[0]
        if any(str(arg).startswith('--files-from=') for arg in argv):
            raise gluetool.GlueCommandError(argv, MagicMock(exit_code=1, stdout='', stderr=''))
        return 'Ok'

    monkeypatch.setattr(gluetool.utils.Command, 'run', _mock_run)

    module.execute()

    batched = _batched_rsync_calls(mock_command_init)
    assert batched

    # Every batched source is retried on the per-file path, on top of the three entries which were
    # never batchable in the first place. The retries reuse the staged, already redacted copies.
    per_file = [argv for argv in _rsync_calls(mock_command_init) if argv not in batched]
    assert len(per_file) == 3 + 4
    assert ['rsync', '--rsync-option', '--timeout=10', '{}/tree/batch-relative/nested-3'.format(tmp_path),
            'https://artifacts.example.com:/artifacts-root/request-id/batch-relative/nested-3'] in per_file

    # The message carries the underlying error, so match on a substring rather than the whole line.
    assert any(
        record.levelno == logging.WARNING
        and 'Batched rsync using ssh failed, falling back to per-file archiving' in record.message
        for record in log.records
    )


@pytest.mark.parametrize('exit_code', [23, 24])
def test_batched_archiving_tolerates_partial(monkeypatch, batched_module, tmp_path, exit_code):
    module = batched_module

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    def _mock_run(*args, **kwargs):
        argv = mock_command_init.call_args.args[0]
        if any(str(arg).startswith('--files-from=') for arg in argv):
            raise gluetool.GlueCommandError(argv, MagicMock(exit_code=exit_code, stdout='', stderr=''))
        return 'Ok'

    monkeypatch.setattr(gluetool.utils.Command, 'run', _mock_run)

    module.execute()

    # Tolerated: a single attempt, no retry and no per-file fallback.
    assert len(_batched_rsync_calls(mock_command_init)) == 1

    fallback = [
        argv for argv in _rsync_calls(mock_command_init)
        if not any(str(arg).startswith('--files-from=') for arg in argv)
        and any('batch-source-1' in str(arg) or 'nested-3' in str(arg) for arg in argv)
    ]
    assert fallback == []


def test_batched_archiving_copy_failure(monkeypatch, batched_module, tmp_path):
    module = batched_module

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    def _mock_copy2(source, target, **kwargs):
        if 'batch-source-1' in source:
            raise FileNotFoundError(source)

    monkeypatch.setattr(shutil, 'copy2', _mock_copy2)

    module.execute()

    # The file which could not be staged is dropped, the rest still transfers.
    assert (tmp_path / 'files-from.txt').read_bytes() == (
        b'archive-excludes/exclude-2\x00batch-relative/nested-3\x00batch-source-2\x00'
    )
    assert len(_batched_rsync_calls(mock_command_init)) == 1


def test_batched_archiving_exclude(monkeypatch, batched_module, tmp_path):
    module = batched_module
    module._config['batch-exclude'] = '*nested-3'

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    assert (tmp_path / 'files-from.txt').read_bytes() == (
        b'archive-excludes/exclude-2\x00batch-source-1\x00batch-source-2\x00'
    )
    assert len(_batched_rsync_calls(mock_command_init)) == 1

    # Kept out of the batch, not out of the archive.
    assert ['rsync', '--rsync-option', '--timeout=10', '{}/nested-3'.format(tmp_path),
            'https://artifacts.example.com:/artifacts-root/request-id/batch-relative/nested-3'] \
        in _rsync_calls(mock_command_init)


def test_batched_archiving_exclude_fallback(monkeypatch, batched_module, tmp_path):
    module = batched_module
    module._config['batch-exclude'] = '*nested-3'

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    def _mock_run(*args, **kwargs):
        argv = mock_command_init.call_args.args[0]
        if any(str(arg).startswith('--files-from=') for arg in argv):
            raise gluetool.GlueCommandError(argv, MagicMock(exit_code=1, stdout='', stderr=''))
        return 'Ok'

    monkeypatch.setattr(gluetool.utils.Command, 'run', _mock_run)

    module.execute()

    # A failed batch must not change what a batch-exclude pattern means: the file is archived exactly
    # once, on its own, whether the batch succeeds or not.
    destinations = [argv[-1] for argv in _rsync_calls(mock_command_init)]
    assert destinations.count('https://artifacts.example.com:/artifacts-root/request-id/batch-relative/nested-3') == 1


def test_batched_archiving_max_files(monkeypatch, batched_module, tmp_path):
    module = batched_module
    module._config['batch-max-files'] = 2

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    # Four batchable files, chunked into two batches of two.
    assert len(_batched_rsync_calls(mock_command_init)) == 2


def test_batched_archiving_cancelled(monkeypatch, batched_module, tmp_path):
    module = batched_module

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    timer = MagicMock()
    timer.finished.is_set.return_value = True
    module._archive_timer = timer

    module.archive_stage('progress')

    assert _batched_rsync_calls(mock_command_init) == []


@pytest.mark.parametrize('enabled', ['no', 'yes'])
def test_batched_archiving_equivalence(monkeypatch, module, tmp_path, enabled):
    """
    The default asset produces no multi-file batch, so the flag must not change a single argv.
    """

    module._config['enable-batched-archiving'] = enabled
    module._config['enable-parallel-archiving'] = False

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: '/tmp/dir')

    module.execute()
    module.destroy()

    assert _rsync_calls(mock_command_init) == [
        ['rsync', '--rsync-option', '--timeout=10', '/tmp/dir/archive-source-execute',
         'https://artifacts.example.com:/artifacts-root/request-id/archive-source-execute'],
        ['rsync', '--rsync-option', '--timeout=10', '/tmp/dir/env-archive-source2',
         'https://artifacts.example.com:/artifacts-root/request-id/env-archive-source2'],
        ['rsync', '--rsync-option', '--timeout=10', '/archive-source',
         'https://artifacts.example.com:/artifacts-root/request-id/dest'],
        ['rsync', '--rsync-option', '--timeout=10', '/archive-source',
         'https://artifacts.example.com:/artifacts-root/request-id/'],
        ['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/archive-source',
         'https://artifacts.example.com:/artifacts-root/request-id/dest'],
        ['rsync', '--rsync-option', '--timeout=10', '--recursive', '/dir-archive-source',
         'https://artifacts.example.com:/artifacts-root/request-id/'],
        ['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/1',
         'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'],
        ['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/2',
         'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'],
        ['rsync', '--rsync-option', '--timeout=10', '/dir-archive-source/3',
         'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'],
        ['rsync', '--rsync-option', '--timeout=10', '/archive-excludes/exclude-2',
         'https://artifacts.example.com:/artifacts-root/request-id/archive-excludes'],
        ['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/env-archive-source',
         'https://artifacts.example.com:/artifacts-root/request-id/env-dest'],
    ]


def test_batched_archiving_deduplicates(monkeypatch, batched_module, tmp_path):
    # Two map entries globbing the same file must stage and list it once.
    def _dup_glob(path, recursive=False):
        if 'batch-source-1' in path or 'batch-source-2' in path:
            return ['/batch-source-1', '/batch-source-1']

        return _mock_glob(path, recursive=recursive)

    mock_command_init = _patch_transfer(monkeypatch, batched_module, lambda: str(tmp_path))
    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _dup_glob)

    batched_module.execute()

    assert (tmp_path / 'files-from.txt').read_bytes() == (
        b'archive-excludes/exclude-2\x00batch-relative/nested-3\x00batch-source-1\x00'
    )
    assert len(_batched_rsync_calls(mock_command_init)) == 1


def test_batch_list_survives_newline_in_path(module, tmp_path):
    # Text mode would translate the newline and split the entry in two.
    relpaths = ['work-1/od\nd/output.txt', 'work-1/plain.txt']

    list_file = module.write_batch_list(str(tmp_path), relpaths)

    assert open(list_file, 'rb').read() == b'work-1/od\nd/output.txt\x00work-1/plain.txt\x00'
    # Splitting on NUL, as --from0 does, recovers both entries intact.
    assert open(list_file, 'rb').read().split(b'\0')[:-1] == [
        b'work-1/od\nd/output.txt', b'work-1/plain.txt'
    ]


@pytest.mark.parametrize('source, batchable', [
    ('../../outside.log', False),
    ('..', False),
    ('work-1/../../outside.log', False),
    ('work-1/../inside.log', True),
    ('/abs/../inside.log', True),
])
def test_batched_archiving_parent_relative_not_batchable(batched_module, source, batchable):
    # A source escaping the working directory would be staged outside of the batch tree, it stays on
    # the per-file path.
    assert batched_module.is_batchable('execute', {}, source) is batchable


def test_prepare_batch_tree_stays_inside(monkeypatch, batched_module, tmp_path):
    batch_root = tmp_path / 'batch'
    batch_root.mkdir()

    workdir = tmp_path / 'work' / 'a' / 'b'
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)

    # `../../outside.log` from `batch/tree` is `tmp_path/outside.log`, an unrelated file.
    (tmp_path / 'work' / 'outside.log').write_text('source')
    (tmp_path / 'outside.log').write_text('victim')

    # A symlink inside the tree, pointing out of it, must not redirect a later copy either.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (batch_root / 'tree').mkdir()
    (batch_root / 'tree' / 'link').symlink_to(elsewhere)
    (workdir / 'link').mkdir()
    (workdir / 'link' / 'file.log').write_text('source')

    (workdir / 'inside.log').write_text('inside')

    tree_root, staged, fallback = batched_module.prepare_batch_tree(
        str(batch_root), [(source, {}) for source in ['../../outside.log', 'link/file.log', 'inside.log']]
    )

    assert list(staged) == ['inside.log']
    assert fallback == [('../../outside.log', {}), ('link/file.log', {})]
    assert (tmp_path / 'outside.log').read_text() == 'victim'
    assert list(elsewhere.iterdir()) == []
    assert (batch_root / 'tree' / 'inside.log').read_text() == 'inside'


def test_batched_archiving_redaction_failure(monkeypatch, batched_module, tmp_path, log):
    module = batched_module

    def _hide_secrets(search_path):
        # Fail the redaction of the whole batch tree, and of one file on the per-file path.
        if search_path == '{}/tree'.format(tmp_path) or 'batch-source-1' in search_path:
            raise gluetool.GlueError('redaction failed')

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(id='request-id'),
        'artifacts_location': lambda path: 'https://artifacts.example.com/{}'.format(path),
        'hide_secrets': _hide_secrets,
    })

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    # Must not raise out of the stage.
    module.execute()

    # The batch whose redaction failed never reached rsync.
    assert _batched_rsync_calls(mock_command_init) == []

    # Every batched file went through the per-file path instead, where only the file whose own
    # redaction failed is missing.
    transferred = [argv[-1] for argv in _rsync_calls(mock_command_init)]
    assert 'https://artifacts.example.com:/artifacts-root/request-id/batch-source-1' not in transferred
    for destination in ['batch-source-2', 'batch-relative/nested-3', 'archive-excludes/exclude-2']:
        assert 'https://artifacts.example.com:/artifacts-root/request-id/{}'.format(destination) in transferred

    assert any(
        record.levelno == logging.WARNING
        and 'Preparing batched archiving failed, falling back to per-file archiving' in record.message
        for record in log.records
    )
    assert any(
        record.levelno == logging.ERROR and 'Failed to sync /batch-source-1' in record.message
        for record in log.records
    )


def test_rsync_filter_options(module):
    module._config['rsync-options'] = [
        '--links', '--exclude=work-*/**/tree/', '--include', 'tests.yaml', '-C', '--filter=- *.tmp',
        '-f', '- *.bak', '-f- *.old', '--times',
    ]

    assert module.rsync_filter_options == [
        '--exclude=work-*/**/tree/', '--include', 'tests.yaml', '-C', '--filter=- *.tmp',
        '-f', '- *.bak', '-f- *.old',
    ]
    assert module.rsync_transfer_options == ['--links', '--times', '--timeout=10']


# The production `rsync-options`, see `citool-config/config/archive` in the infrastructure repository,
# plus a rule which matches a file name, so the test also proves the batch still applies such rules.
PRODUCTION_RSYNC_OPTIONS = [
    '--links', '--times', '--verbose', '--compress', '--checksum',
    '--exclude=work-*/**/tree/',
    '--include=work-*/**/discover/tests.yaml',
    '--exclude=work-*/**/discover/**',
    '--exclude=*.tmp',
]

REAL_RSYNC_TREE = {
    'pipeline.log': 'pipeline',
    'work-1/plan/discover/output.txt': 'discover',
    'work-1/plan/discover/tests.yaml': 'tests',
    'work-1/plan/execute/data/guest/default-0/test-1/output.txt': 'execute',
    'work-1/plan/tree/output.txt': 'tree',
    'work-1/plan/execute/output.tmp': 'excluded by name',
}


def _archived_files(root):
    return {
        os.path.relpath(os.path.join(dirpath, name), root): open(os.path.join(dirpath, name)).read()
        for dirpath, _, names in os.walk(root)
        for name in names
    }


@pytest.mark.skipif(shutil.which('rsync') is None, reason='requires rsync')
@pytest.mark.parametrize('enabled', ['no', 'yes'])
@pytest.mark.parametrize('rsync_options, excluded_suffix', [
    (PRODUCTION_RSYNC_OPTIONS, '.tmp'),
    # Flattening the batched paths would also let the `output.txt` files overwrite each other.
    (PRODUCTION_RSYNC_OPTIONS + ['--no-relative'], '.tmp'),
    # `!` clears every rule before it, the path based exclude after it still matches nothing by name.
    (['--links', '--times', '--exclude=*.tmp', '--filter=!', '--exclude=work-*/**/discover/**'], None),
], ids=['production', 'no-relative', 'filter-clear'])
def test_batched_archiving_real_rsync_filters(
    monkeypatch, batched_module, tmp_path, enabled, rsync_options, excluded_suffix
):
    """
    With a real rsync and the given rules, batching must archive exactly the files the per-file path
    archives. The per-file path matches the rules against the file name only, so the path based rules
    never exclude anything there, while `*.tmp` does.
    """

    module = batched_module

    source_root = tmp_path / 'source'
    for relpath, content in REAL_RSYNC_TREE.items():
        (source_root / relpath).parent.mkdir(parents=True, exist_ok=True)
        (source_root / relpath).write_text(content)

    map_file = tmp_path / 'map.yaml'
    map_file.write_text(
        'execute:\n'
        '  - source: "*.log"\n'
        '  - source: "work-*/**/output.txt"\n'
        '  - source: "work-*/**/tests.yaml"\n'
        '  - source: "work-*/**/*.tmp"\n'
    )

    module._config['source-destination-map'] = str(map_file)
    module._config['archive-mode'] = 'local'
    module._config['artifacts-local-root'] = str(tmp_path / 'artifacts')
    module._config['rsync-options'] = rsync_options
    module._config['enable-batched-archiving'] = enabled

    commands = []
    original_command_init = gluetool.utils.Command.__init__

    def _command_init(self, executable, *args, **kwargs):
        commands.append(executable)
        original_command_init(self, executable, *args, **kwargs)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', _command_init)
    monkeypatch.chdir(source_root)

    module.execute()

    expected = {
        relpath: content for relpath, content in REAL_RSYNC_TREE.items()
        if excluded_suffix is None or not relpath.endswith(excluded_suffix)
    }

    assert _archived_files(str(tmp_path / 'artifacts' / 'request-id')) == expected

    batched = [argv for argv in commands if any(arg.startswith('--files-from=') for arg in argv)]
    # The filter dry run, and the transfer itself.
    assert len(batched) == (2 if enabled == 'yes' else 0)


@pytest.mark.skipif(shutil.which('rsync') is None, reason='requires rsync')
def test_rsync_filter_names_non_ascii(monkeypatch, module, tmp_path):
    # Under a non-UTF-8 locale rsync escapes non-ASCII bytes in `%n`, unless told otherwise.
    monkeypatch.setenv('LC_ALL', 'C')
    module._config['rsync-options'] = '--exclude=*.tmp'

    assert module.rsync_filter_names(
        str(tmp_path / 'filter'), ['plain.txt', 'v\u00fdstup.txt', 'drop.tmp']
    ) == ['plain.txt', 'v\u00fdstup.txt']


def test_prepare_batch_tree_last_source_wins(monkeypatch, batched_module, tmp_path):
    # `/<tmp>/example.log` and `<tmp>/example.log` read different files, but share a destination. Per
    # file, the source globbed last overwrites the other one, a batch must stage that one.
    absolute = tmp_path / 'example.log'
    absolute.write_text('first')

    workdir = tmp_path / 'cwd'
    relative = Path(str(absolute).lstrip('/'))
    (workdir / relative).parent.mkdir(parents=True)
    (workdir / relative).write_text('second')
    monkeypatch.chdir(workdir)

    batch_root = tmp_path / 'batch'
    batch_root.mkdir()

    tree_root, staged, fallback = batched_module.prepare_batch_tree(
        str(batch_root), [(str(absolute), {}), (str(relative), {})]
    )

    assert staged == {str(relative): (str(relative), {})}
    assert fallback == []
    assert (Path(tree_root) / relative).read_text() == 'second'


def test_batched_archiving_s3(monkeypatch, batched_module, tmp_path):
    module = batched_module
    module._config['archive-mode'] = 'ssh,s3'

    mock_hide_secrets = MagicMock()
    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(id='request-id'),
        'artifacts_location': lambda path: 'https://artifacts.example.com/{}'.format(path),
        'hide_secrets': mock_hide_secrets,
    })

    mock_command_init = _patch_transfer(monkeypatch, module, lambda: str(tmp_path))

    module.execute()

    # One batch for the rsync mode, and the same staged, redacted files uploaded one by one to S3.
    assert len(_batched_rsync_calls(mock_command_init)) == 1
    assert mock_hide_secrets.call_args_list.count(call(search_path='{}/tree'.format(tmp_path))) == 1

    aws = [c.args[0] for c in mock_command_init.call_args_list if c.args and c.args[0][:2] == ['aws', 's3']]
    for relpath in ['archive-excludes/exclude-2', 'batch-relative/nested-3', 'batch-source-1', 'batch-source-2']:
        assert ['aws', 's3', 'cp', '--aws-option', '{}/tree/{}'.format(tmp_path, relpath),
                's3://aws-s3-bucket/artifacts-root/request-id/{}'.format(relpath)] in aws


def test_batch_chunks_max_size(module, tmp_path):
    module._config['batch-max-size'] = 1

    sources = []
    for name in ['a', 'b', 'c']:
        (tmp_path / name).write_bytes(b'x' * 400 * 1024)
        sources.append((str(tmp_path / name), {}))

    # 400 KiB each, two of them fit into 1 MiB, the third does not.
    assert module.batch_chunks(sources) == [sources[:2], sources[2:]]

    module._config['batch-max-size'] = 0
    module._config['batch-max-files'] = 0

    assert module.batch_chunks(sources) == [sources]
