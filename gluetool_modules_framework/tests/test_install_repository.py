# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from mock import MagicMock, call

from gluetool.utils import Command
import gluetool_modules_framework.libs.guest_setup
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.helpers.install_repository
import gluetool_modules_framework.helpers.rules_engine
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

from . import create_module, patch_shared


def mock_guest(execute_mock):
    guest_mock = MagicMock()
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock

    return guest_mock


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.helpers.install_repository.InstallRepository)[1]

    module._config['log-dir-name'] = 'log-dir-example'
    module._config['download-path'] = 'dummy-path'

    def dummy_testing_farm_request():
        environments_requested = [
            TestingEnvironment(artifacts=[
                Artifact(id='https://example.com/repo1', packages=None, type='repository'),
                Artifact(id='https://example.com/repo2', packages=None, type='repository'),
                Artifact(id='https://example.com/repo3', packages=['package-install-1', 'package2'], type='repository'),
                Artifact(id='https://example.com/repo4.repo', type='repository-file'),
                Artifact(id='wrongid', packages=None, type='wongtype'),
            ]),
            TestingEnvironment(artifacts=[Artifact(id='wrongid', packages=None, type='wongtype')]),
        ]
        return MagicMock(environments_requested=environments_requested)

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': dummy_testing_farm_request,
        'evaluate_instructions': gluetool_modules_framework.helpers.rules_engine.RulesEngine.evaluate_instructions,
        'setup_guest': None
    })

    return module


def test_sanity_shared(module):
    assert module.glue.has_shared('setup_guest') is True


@pytest.mark.parametrize('environment_index', [0, 1], ids=['multiple-repositories', 'no-repositories'])
def test_guest_setup(module, environment_index, tmpdir, monkeypatch):
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    mock_output = MagicMock(
        exit_code=0, stdout='https://example.com/package-install-1.0.1.rpm\nhttps://example.com/dummy3.rpm\nhttps://example.com/package-install-1.0.2.rpm', stderr='')
    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(Command, '__init__', mock_command_init)
    monkeypatch.setattr(Command, 'run', mock_command_run)

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock)
    guest.environment = module.shared('testing_farm_request').environments_requested[environment_index]
    guest.environment.excluded_packages = ['dummy3']

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    if environment_index == 1:
        execute_mock.assert_has_calls([])
        return

    command_calls = [
        call(['dnf', 'repoquery', '-q', '--queryformat', '"%{{name}}"', '--repofrompath=artifacts-repo,https://example.com/repo1', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['dnf', 'repoquery', '-q', '--queryformat', '"%{{name}}"', '--repofrompath=artifacts-repo,https://example.com/repo2', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['dnf', 'repoquery', '-q', '--queryformat', '"%{{name}}"', '--repofrompath=artifacts-repo,https://example.com/repo3', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
    ]
    mock_command_init.assert_has_calls(command_calls)

    execute_calls = [
        call('command -v dnf'),
        call('curl --output /etc/yum.repos.d/repo4.repo.repo -LO https://example.com/repo4.repo'),
        call('mkdir -pv dummy-path'),
        call('cd dummy-path; echo https://example.com/dummy3.rpm https://example.com/package-install-1.0.2.rpm https://example.com/dummy3.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm | xargs -n1 curl -sO'),
        call('dnf -y reinstall https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm'),
        call('dnf -y downgrade --allowerasing https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm'),
        call('dnf -y update --allowerasing https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm'),
        call('dnf -y install --allowerasing https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm'),
        call('basename --suffix=.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm https://example.com/package-install-1.0.2.rpm | xargs rpm -q')
    ]

    execute_mock.assert_has_calls(execute_calls)
