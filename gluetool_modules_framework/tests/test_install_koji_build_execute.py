# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os

import pytest

from mock import MagicMock, call

import gluetool
import gluetool_modules_framework.libs.guest as guest_module
import gluetool_modules_framework.libs.guest_setup
from gluetool_modules_framework.libs.sut_installation import INSTALL_COMMANDS_FILE
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.helpers.install_koji_build_execute
from gluetool_modules_framework.helpers.install_copr_build import InstallCoprBuild
import gluetool_modules_framework.helpers.rules_engine

from . import create_module, patch_shared


def mock_guest(execute_mock, artifacts=None, environment=None):
    guest_mock = MagicMock()
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock
    guest_mock.environment = environment or TestingEnvironment()
    if artifacts:
        guest_mock.environment.artifacts = artifacts

    return guest_mock


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.helpers.install_koji_build_execute.InstallKojiBuildExecute)[1]

    module._config['log-dir-name'] = 'log-dir-example'

    def dummy_testing_farm_request():
        environments_requested = [
            TestingEnvironment(artifacts=[
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
            ]),
            TestingEnvironment(artifacts=[
                {
                    'id': 'wrongid',
                    'packages': None,
                    'type': 'wongtype'
                }
            ]),
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
    guest.environment = TestingEnvironment(
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


def test_guest_setup(module, local_guest, tmpdir):
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock)

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    commands = [
        'koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch src 123123123',  # noqa
        'brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch src 123123124',  # noqa
        'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | awk "{print \\$2}" | tee rpms-list',  # noqa
        'dnf -y reinstall $(cat rpms-list) || true',
        'dnf -y install --allowerasing $(cat rpms-list)',
        "sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\\n' | xargs -d'\\n' rpm -q"
    ]

    calls = [call('command -v dnf')] * 2 + [call(c) for c in commands]
    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == len(calls)

    with open(os.path.join(str(tmpdir), 'log-dir-example-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == '\n'.join(commands) + '\n'


def test_guest_setup_with_copr(module, local_guest, monkeypatch, tmpdir):
    # both this and the copr module record install commands; make sure that they both work together,
    # and don't overwrite each other
    copr_module = create_module(InstallCoprBuild)[1]
    copr_module._config['log-dir-name'] = 'log-dir-example'
    copr_module._config['download-path'] = 'some-download-path'

    primary_task_mock = MagicMock()
    primary_task_mock.repo_url = 'dummy_repo_url'
    primary_task_mock.rpm_urls = ['dummy_rpm_url1', 'dummy_rpm_url2']
    primary_task_mock.srpm_urls = ['dummy_srpm_url1', 'dummy_srpm_url2']
    primary_task_mock.rpm_names = ['dummy_rpm_names1', 'dummy_rpm_names2']
    primary_task_mock.project = 'dummy_project'

    def tasks_mock(task_ids=None):
        return [primary_task_mock]

    patch_shared(monkeypatch, copr_module, {
        'setup_guest': None
    }, callables={
        'tasks': tasks_mock
    })

    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock, artifacts=[
        {'type': 'fedora-copr-build', 'id': 'artifact1'},
    ])

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))
    copr_module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    koji_commands = [
        'koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch src 123123123',  # noqa
        'brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch src 123123124',  # noqa
        'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | awk "{print \\$2}" | tee rpms-list',  # noqa
        'dnf -y reinstall $(cat rpms-list) || true',
        'dnf -y install --allowerasing $(cat rpms-list)',
        "sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\\n' | xargs -d'\\n' rpm -q"
    ]

    copr_commands = [
        'mkdir -pv some-download-path',
        'curl -v dummy_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-dummy_project-1.repo',
        'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" dummy_rpm_url1 dummy_rpm_url2 dummy_srpm_url1 dummy_srpm_url2',  # noqa
        'dnf -y reinstall dummy_rpm_url1 || true',
        'dnf -y reinstall dummy_rpm_url2 || true',
        'dnf -y install --allowerasing dummy_rpm_url1 dummy_rpm_url2',
        'rpm -q dummy_rpm_names1',
        'rpm -q dummy_rpm_names2',
    ]

    calls = [call('command -v dnf')] * 2 + [call(c) for c in koji_commands]
    calls += [call('command -v dnf')] * 2 + [call(c) for c in copr_commands]
    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == len(calls)

    with open(os.path.join(str(tmpdir), 'log-dir-example-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == '\n'.join(koji_commands + copr_commands) + '\n'


def test_guest_setup_yum(module, local_guest, tmpdir):
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def execute_mock_side_effect(cmd):
        if cmd == 'command -v dnf':
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        return MagicMock(stdout='', stderr='')

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    execute_mock.side_effect = execute_mock_side_effect
    guest = mock_guest(execute_mock)
    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

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
