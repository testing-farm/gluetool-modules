# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import glob
import time
import pytest
import logging
import shutil
import tempfile
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
        '--files-from={}/files-from.txt'.format(tmp_path), '--from0',
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
    # never batchable in the first place.
    per_file = [argv for argv in _rsync_calls(mock_command_init) if argv not in batched]
    assert len(per_file) == 3 + 4

    # The message carries the underlying error, so match on a substring rather than the whole line.
    assert any(
        record.levelno == logging.WARNING
        and 'Batched rsync failed, falling back to per-file archiving' in record.message
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
