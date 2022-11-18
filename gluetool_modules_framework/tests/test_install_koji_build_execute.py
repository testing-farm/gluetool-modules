# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from mock import MagicMock, call

import gluetool
import gluetool_modules_framework.libs.guest as guest_module
import gluetool_modules_framework.libs.guest_setup
import gluetool_modules_framework.libs.testing_environment
import gluetool_modules_framework.helpers.install_koji_build_execute
import gluetool_modules_framework.helpers.rules_engine

from . import create_module, patch_shared


def mock_guest(execute_mock):
    guest_mock = MagicMock()
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock

    return guest_mock


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.helpers.install_koji_build_execute.InstallKojiBuildExecute)[1]

    module._config['log-dir-name'] = 'log-dir-example'

    def dummy_testing_farm_request():
        environments_requested = [
            {
                'artifacts': [
                    {
                        'id': '123123123',
                        'packages': None,
                        'type': 'fedora-koji-build'
                    },
                    {
                        'id': '123123124',
                        'packages': None,
                        'type': 'redhat-brew-build'
                    },
                    {
                        'id': 'wrongid',
                        'packages': None,
                        'type': 'wongtype'
                    }
                ]
            },
            {
                'artifacts': [
                    {
                        'id': 'wrongid',
                        'packages': None,
                        'type': 'wongtype'
                    }
                ]
            }
        ]
        return MagicMock(environments_requested=environments_requested)

    def evaluate_instructions_mock(workarounds, callbacks):
        callbacks['steps']('instructions', 'commands', workarounds, 'context')

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': dummy_testing_farm_request,
        'evaluate_instructions': evaluate_instructions_mock,
        'setup_guest': None
    })

    return module


@pytest.fixture(name='local_guest')
def fixture_local_guest(module):
    guest = guest_module.NetworkedGuest(module, '127.0.0.1', key=MagicMock())
    guest.execute = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest.environment = gluetool_modules_framework.libs.testing_environment.TestingEnvironment(
        arch='x86_64',
        compose='dummy-compose'
    )

    return guest


def test_sanity_shared(module):
    assert module.glue.has_shared('setup_guest') is True


def test_setup_guest(module, local_guest):
    pass


def test_execute(module, local_guest, monkeypatch):
    module.execute()

    assert module.request_artifacts == [
        {
            'id': '123123123',
            'packages': None,
            'type': 'fedora-koji-build'
        },
        {
            'id': '123123124',
            'packages': None,
            'type': 'redhat-brew-build'
        }
    ]


def test_guest_setup(module, local_guest):
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock)

    module.setup_guest(guest, stage=stage)

    calls = [
        call('command -v dnf'),
        call('koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch src 123123123'),  # noqa
        call('brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch src 123123124'),  # noqa
        call('ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | awk "{print \\$2}" | tee rpms-list'),  # noqa
        call('dnf --allowerasing -y reinstall $(cat rpms-list) || true'),
        call('dnf --allowerasing -y install $(cat rpms-list)'),
        call("sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\\n' | xargs -d'\\n' rpm -q")
    ]

    execute_mock.assert_has_calls(calls)


def test_guest_setup_yum(module, local_guest):
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def execute_mock_side_effect(cmd):
        if cmd == 'command -v dnf':
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        return MagicMock(stdout='', stderr='')

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    execute_mock.side_effect = execute_mock_side_effect
    guest = mock_guest(execute_mock)

    module.setup_guest(guest, stage=stage)

    calls = [
        call('command -v dnf'),
        call('koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch src 123123123'),  # noqa
        call('brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch src 123123124'),  # noqa
        call('ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | awk "{print \\$2}" | tee rpms-list'),  # noqa
        call('yum -y reinstall $(cat rpms-list)'),
        call('yum -y downgrade $(cat rpms-list)'),
        call('yum -y install $(cat rpms-list)'),
        call("sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\\n' | xargs -d'\\n' rpm -q")
    ]

    execute_mock.assert_has_calls(calls)
