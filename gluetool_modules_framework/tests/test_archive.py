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

    return glob.glob(path, recursive=recursive)


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
