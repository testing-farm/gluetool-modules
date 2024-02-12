# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import glob
import time
import pytest
import logging
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
    module._config['source-destination-map'] = '{}/source-destination-map.yaml'.format(ASSETS_DIR)
    module._config['rsync-mode'] = 'ssh'
    module._config['rsync-options'] = '--rsync-option'
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 5
    module._config['rsync-timeout'] = 10

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(id='request-id'),
    })

    os.environ['SOURCE_DESTINATION_MAP'] = '/env-archive-source:env-dest:666:destroy#/env-archive-source2:::execute'

    return module


def _mock_glob(path):
    if '*' in path:
        return ['/archive-source/1', '/archive-source/2', '/archive-source/3']
    if 'archive-source' in path:
        return [path]

    return glob.glob(path)


def test_sanity(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/archive.py', 'Archive')

    module._config['rsync-mode'] = 'invalid'

    with pytest.raises(gluetool.GlueError, match='rsync mode must be either daemon or ssh'):
        module.sanity()

    module._config['rsync-mode'] = 'daemon'
    module._config['artifacts-rsync-host'] = None
    with pytest.raises(gluetool.GlueError, match='rsync daemon host must be specified when using rsync daemon mode'):
        module.sanity()

    module._config['rsync-mode'] = 'ssh'
    module._config['artifacts-host'] = None
    with pytest.raises(gluetool.GlueError, match='artifacts host must be specified when using ssh mode'):
        module.sanity()


def test_execute_destroy_ssh(monkeypatch, module):
    module._config['enable-parallel-archiving'] = False

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    monkeypatch.setattr(os.path, 'exists', lambda x: True)

    def _isdir(path):
        if path == '/dir-archive-source':
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

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source-execute',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-source-execute'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--recursive', '/dir-archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/dir-archive-source'], logger=module.logger),

        call(['ssh', 'https://artifacts.example.com', 'mkdir', '-p',
              '/artifacts-root/request-id/archive-source'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source/1',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-source/1'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source/2',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-source/2'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source/3',
              'https://artifacts.example.com:/artifacts-root/request-id/archive-source/3'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/env-archive-source',
              'https://artifacts.example.com:/artifacts-root/request-id/env-dest'], logger=module.logger),

        call(['rsync', '--rsync-option', '--timeout=10', '/env-archive-source2',
              'https://artifacts.example.com:/artifacts-root/request-id/env-archive-source2'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)


def test_destroy_daemon(monkeypatch, module):
    module._config['rsync-mode'] = 'daemon'

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    monkeypatch.setattr(os.path, 'exists', lambda x: True)

    def _isdir(path):
        if path == '/dir-archive-source':
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    monkeypatch.setattr(gluetool_modules_framework.helpers.archive, 'glob', _mock_glob)

    module.execute()

    module.destroy()

    calls = [
        call(['rsync', '--rsync-option', '--timeout=10', '/dev/null',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'rsync://artifacts-rsync.example.com/request-id/dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source',
              'rsync://artifacts-rsync.example.com/request-id/archive-source'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/archive-source',
              'rsync://artifacts-rsync.example.com/request-id/dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '--recursive', '/dir-archive-source',
              'rsync://artifacts-rsync.example.com/request-id/dir-archive-source'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '/dev/null',
              'rsync://artifacts-rsync.example.com/request-id/archive-source/'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source/1',
              'rsync://artifacts-rsync.example.com/request-id/archive-source/1'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source/2',
              'rsync://artifacts-rsync.example.com/request-id/archive-source/2'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '/archive-source/3',
              'rsync://artifacts-rsync.example.com/request-id/archive-source/3'], logger=module.logger),
        call(['rsync', '--rsync-option', '--timeout=10', '--chmod=666', '/env-archive-source',
              'rsync://artifacts-rsync.example.com/request-id/env-dest'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)


def test_parallel_archiving(monkeypatch, module, log):
    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    monkeypatch.setattr(os.path, 'exists', lambda x: True)

    def _isdir(path):
        if path == '/dir-archive-source':
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
    assert log.match(levelno=logging.DEBUG,
                     message='syncing /archive-source-progress to https://artifacts.example.com:/artifacts-root/request-id/archive-source-progress')
