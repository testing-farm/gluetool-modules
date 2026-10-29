# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import glob
import time
import pytest
import logging
import shutil
import tempfile
from mock import ANY, MagicMock, call, patch

import gluetool
import gluetool_modules_framework.helpers.archive
import gluetool_modules_framework.libs.file_stat
from gluetool_modules_framework.helpers.archive import Archive
from gluetool_modules_framework.helpers.hide_secrets import HideSecrets

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

    with pytest.raises(gluetool.GlueError, match='rsync mode must be either daemon, ssh, local or s3'):
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


def test_execute_destroy_ssh(monkeypatch, module):
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


def test_destroy_daemon(monkeypatch, module):
    module._config['archive-mode'] = 'daemon'

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


def test_execute_destroy_local(monkeypatch, module):
    module._config['enable-parallel-archiving'] = False
    module._config['archive-mode'] = 'local'

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


def test_execute_destroy_s3(monkeypatch, module):
    module._config['enable-parallel-archiving'] = False
    module._config['archive-mode'] = 's3'

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


def test_parallel_archiving(monkeypatch, module, log):
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
        message='syncing /tmp/dir/archive-source-progress to https://artifacts.example.com:/artifacts-root/request-id/archive-source-progress'  # Ignore PEP8Bear
    )
    assert log.match(
        levelno=logging.DEBUG,
        message='syncing /tmp/dir/archive-source-another-progress to https://artifacts.example.com:/artifacts-root/request-id/archive-source-another-progress'  # Ignore PEP8Bear
    )

    mock_shutil_copytree.assert_called_with(
        '/archive-source-another-progress', '/tmp/dir/archive-source-another-progress',
        symlinks=True, ignore_dangling_symlinks=True, dirs_exist_ok=True, copy_function=ANY
    )
    mock_shutil_rmtree.assert_called_with('/tmp/dir')

    mock_shutil_copy2.assert_called_with(
        '/archive-source-progress', '/tmp/dir/archive-source-progress', follow_symlinks=False
    )


@pytest.mark.parametrize('archive_mode', ['ssh', 's3'])
@pytest.mark.parametrize('source_copy, expected_kwargs', [
    # A copy is redacted on behalf of the original it was taken from, so that hide-secrets can
    # recognise the original's unchanged files the next time the same source is copied.
    (True, {'search_path': '/tmp/dir/archive-source', 'cache_path': '/archive-source', 'source_stats': {}}),
    (False, {'search_path': '/archive-source'}),
])
def test_hide_secrets_cache_path(monkeypatch, module, archive_mode, source_copy, expected_kwargs):
    module._config['archive-mode'] = archive_mode

    mock_hide_secrets = MagicMock()

    monkeypatch.setattr(gluetool.utils.Command, '__init__', MagicMock(return_value=None))
    monkeypatch.setattr(gluetool.utils.Command, 'run', MagicMock(return_value='Ok'))
    monkeypatch.setattr(shutil, 'copy2', MagicMock())
    monkeypatch.setattr(shutil, 'rmtree', MagicMock())
    monkeypatch.setattr(tempfile, 'mkdtemp', lambda: '/tmp/dir')
    monkeypatch.setattr(os.path, 'exists', lambda _: True)
    monkeypatch.setattr(os.path, 'isdir', lambda _: False)

    patch_shared(monkeypatch, module, {}, callables={
        'hide_secrets': mock_hide_secrets,
    })

    module._request_id = 'request-id'

    if archive_mode == 's3':
        module.run_aws('/archive-source', 'dest', source_copy=source_copy)
    else:
        module.run_rsync('/archive-source', 'dest', source_copy=source_copy)

    mock_hide_secrets.assert_called_once_with(**expected_kwargs)


@pytest.fixture(name='settled')
def fixture_settled(monkeypatch):
    # Files a test has just written count as settled, so that hide-secrets caches them at all.
    monkeypatch.setattr(gluetool_modules_framework.libs.file_stat, 'CACHE_SETTLE_TIME', 0)


def _rewrite_during_copy(path, contents):
    """
    Make `shutil.copy2` rewrite `path` with `contents`, of the same size, and advance its mtime,
    right after copying its data and before copying its metadata.
    """
    real_copyfile = shutil.copyfile

    def _copyfile(src, dst, *args, **kwargs):
        result = real_copyfile(src, dst, *args, **kwargs)

        if src == path:
            stat_result = os.stat(path)

            with open(path, 'w') as f:
                f.write(contents)

            os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1000000000))

        return result

    return patch.object(shutil, 'copyfile', side_effect=_copyfile)


def test_prepare_source_copy_source_stats(module, settled):
    with tempfile.TemporaryDirectory() as tmpdir:
        original = os.path.join(tmpdir, 'work-dir')
        os.makedirs(os.path.join(original, 'sub'))

        quiet = os.path.join(original, 'sub', 'quiet.log')
        with open(quiet, 'w') as f:
            f.write('quiet\n')

        live = os.path.join(original, 'live.log')
        with open(live, 'w') as f:
            f.write('public\n')

        os.symlink('live.log', os.path.join(original, 'link.log'))

        with _rewrite_during_copy(live, 'secret\n'):
            source, original_source, source_stats = module.prepare_source_copy(original + '/')

        try:
            assert original_source == original

            # The copy of `live.log` holds bytes the original no longer has, so it gets no stat.
            assert source_stats == {quiet: gluetool_modules_framework.libs.file_stat.settled_file_stat(quiet)}

            with open(os.path.join(source, 'live.log')) as f:
                assert f.read() == 'public\n'

        finally:
            module.delete_source_copy(source)


def test_prepare_source_copy_skips_unsettled_files(module):
    # A write in the same clock tick as the previous one would not change the stat.
    with tempfile.TemporaryDirectory() as tmpdir:
        original = os.path.join(tmpdir, 'fresh.log')
        with open(original, 'w') as f:
            f.write('just written\n')

        source, _, source_stats = module.prepare_source_copy(original)

        try:
            assert source_stats == {}

        finally:
            module.delete_source_copy(source)


@pytest.mark.parametrize('archive_mode', ['ssh', 's3'])
def test_source_changing_while_copied_is_redacted_next_tick(monkeypatch, module, settled, archive_mode):
    # The original changes between `shutil.copy2` copying its data and its metadata. The first
    # copy then holds the old clean bytes with the new mtime. If hide-secrets remembered that
    # as clean, it would skip the next copy, which holds the secret with the same stat.
    module._config['archive-mode'] = archive_mode

    _, hide_secrets = create_module(HideSecrets)
    hide_secrets._config['implementation'] = 'stream'
    hide_secrets._config['retry-tick'] = 1
    hide_secrets._config['retry-timeout'] = 5
    hide_secrets.add_secrets('SECRET')

    patch_shared(monkeypatch, module, {}, callables={
        'hide_secrets': hide_secrets.hide_secrets,
    })

    uploads = []

    def _upload(cmd, logger=None):
        uploads.append(cmd)

    def _run(*args, **kwargs):
        with open(uploads[-1][-2]) as f:
            uploads[-1] = f.read()

    monkeypatch.setattr(gluetool.utils.Command, '__init__', lambda self, cmd, logger=None: _upload(cmd))
    monkeypatch.setattr(gluetool.utils.Command, 'run', _run)

    module._request_id = 'request-id'

    with tempfile.TemporaryDirectory() as tmpdir:
        original = os.path.join(tmpdir, 'console.log')
        with open(original, 'w') as f:
            f.write('CLEAN!')

        run = module.run_aws if archive_mode == 's3' else module.run_rsync

        with _rewrite_during_copy(original, 'SECRET'):
            run(original, '', source_copy=True)

        run(original, '', source_copy=True)
        run(original, '', source_copy=True)

    assert uploads == ['CLEAN!', 'hidden', 'hidden']
