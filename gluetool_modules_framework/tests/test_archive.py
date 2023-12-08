# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import time
import pytest
import logging
from mock import MagicMock, call

import gluetool
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

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(id='request-id'),
    })

    os.environ['SOURCE_DESTINATION_MAP'] = '/env-source:env-dest:666#/env-source2::'

    return module


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


def test_destroy_ssh(monkeypatch, module):
    module._config['enable-parallel-archiving'] = False

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    monkeypatch.setattr(os.path, 'exists', lambda x: True)

    def _isdir(path):
        if path == '/dir-source':
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    # run execute to test directory creation
    module.execute()

    module.destroy()

    calls = [
        call(['ssh', 'https://artifacts.example.com', 'mkdir', '-p',
              '/artifacts-root/request-id'], logger=module.logger),
        call(['rsync', '--rsync-option', '/source',
              'https://artifacts.example.com:/artifacts-root/request-id/dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '/source',
              'https://artifacts.example.com:/artifacts-root/request-id/'], logger=module.logger),
        call(['rsync', '--rsync-option', '--chmod=666', '/source',
              'https://artifacts.example.com:/artifacts-root/request-id/dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '--recursive', '/dir-source',
              'https://artifacts.example.com:/artifacts-root/request-id/'], logger=module.logger),
        call(['rsync', '--rsync-option', '--chmod=666', '/env-source',
              'https://artifacts.example.com:/artifacts-root/request-id/env-dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '/env-source2',
              'https://artifacts.example.com:/artifacts-root/request-id/'], logger=module.logger),
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
        if path == '/dir-source':
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)

    module.destroy()

    calls = [
        call(['rsync', '--rsync-option', '/source',
              'rsync://artifacts-rsync.example.com/request-id/dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '/source',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),
        call(['rsync', '--rsync-option', '--chmod=666', '/source',
              'rsync://artifacts-rsync.example.com/request-id/dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '--recursive', '/dir-source',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),
        call(['rsync', '--rsync-option', '--chmod=666', '/env-source',
              'rsync://artifacts-rsync.example.com/request-id/env-dest'], logger=module.logger),
        call(['rsync', '--rsync-option', '/env-source2',
              'rsync://artifacts-rsync.example.com/request-id/'], logger=module.logger),
    ]

    mock_command_init.assert_has_calls(calls, any_order=True)


def test_parallel_archiving(monkeypatch, module, log):
    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value='Ok')

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    monkeypatch.setattr(os.path, 'exists', lambda x: True)

    def _isdir(path):
        if path == '/dir-source':
            return True

        return False

    monkeypatch.setattr(os.path, 'isdir', _isdir)
    module._config['enable-parallel-archiving'] = True
    module._config['parallel-archiving-tick'] = 0.1

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message == 'Starting parallel archiving, run every 0.1 seconds'

    # make sure the timer runs
    time.sleep(0.2)

    module.destroy()

    assert log.match(levelno=logging.INFO, message='Stopping parallel archiving')
