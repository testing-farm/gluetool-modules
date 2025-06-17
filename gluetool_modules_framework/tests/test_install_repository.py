# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import tempfile

from mock import MagicMock, call
from gluetool_modules_framework.tests.test_libs_repo import generate_calls as generate_createrepo_calls

import gluetool
from gluetool import GlueError
from gluetool.utils import Command
from gluetool.tests import Bunch
import gluetool_modules_framework.libs.guest_setup
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
import gluetool_modules_framework.helpers.install_repository
import gluetool_modules_framework.helpers.rules_engine
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

from . import create_module, patch_shared


def mock_guest(execute_mock):
    guest_mock = MagicMock()
    guest_mock.name = 'guest0'
    guest_mock.hostname = 'guest0'
    guest_mock.key = 'guest-key'
    guest_mock.port = 22

    guest_mock.execute = execute_mock

    return guest_mock


def get_execute_mock():
    def side_effect(cmd):
        if 'bootc' in cmd:
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        else:
            return MagicMock(stdout='', stderr='')

    return MagicMock(side_effect=side_effect)


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.helpers.install_repository.InstallRepository)[1]

    module._config['log-dir-name'] = 'log-dir-example'
    module._config['download-path'] = 'dummy-path'
    module._config['packages-amount-threshold'] = 50

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
        'setup_guest': None,
        'tmt_command': lambda: ['tmt']
    })

    return module


def test_sanity_shared(module):
    assert module.glue.has_shared('setup_guest') is True


@pytest.mark.parametrize('environment_index', [0, 1], ids=['multiple-repositories', 'no-repositories'])
def test_guest_setup(module, environment_index, tmpdir, monkeypatch):
    mock_file = Bunch(name='dummy.txt', write=MagicMock(), flush=MagicMock())

    # must implement context protocol, I cannot get it to work with Bunch :/
    class MockNamedTemporaryFile(Bunch):
        # pylint: disable=too-few-public-methods
        def __enter__(self):
            return mock_file

        def __exit__(self, *args, **kwargs):
            pass

    mock_tempfile = MockNamedTemporaryFile

    monkeypatch.setattr(gluetool_modules_framework.helpers.install_repository, 'NamedTemporaryFile', mock_tempfile)
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def mock_run_output(stdout):
        return MagicMock(
            exit_code=0,
            stdout=stdout,
            stderr=''
        )

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(side_effect=[
        # Artifact(id='https://example.com/repo1', packages=None, type='repository')
        mock_run_output(''),
        # Artifact(id='https://example.com/repo2', packages=None, type='repository')
        # dummy2 shout not be installed, as we will exclude it, but src.rpm should be downloaded
        mock_run_output('\n'.join([
            'https://example.com/dummy1-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy2-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy2-1.0.1-1.src.rpm'
        ])),
        # Artifact(id='https://example.com/repo3', packages=['package-install-1'], type='repository')
        # * package-noinstall should be ignored completely, including src.rpm
        # * package-install only latest NVR should be installed - package-install-1.0.3.rpm
        mock_run_output('\n'.join([
            'https://example.com/package-install-1.0.1-1.x86_64.rpm',
            'https://example.com/package-install-1.0.1-1.src.rpm',
            'https://example.com/package-install-1.0.3-1.x86_64.rpm',
            'https://example.com/package-install-1.0.3-1.src.rpm',
            'https://example.com/package-install-1.0.2-1.x86_64.rpm',
            'https://example.com/package-install-1.0.2-1.src.rpm',
            'https://example.com/package-noinstall-1.0.1-1.x86_64.rpm',
            'https://example.com/package-noinstall-1.0.1-1.src.rpm'
        ])),
    ])

    monkeypatch.setattr(Command, '__init__', mock_command_init)
    monkeypatch.setattr(Command, 'run', mock_command_run)

    execute_mock = get_execute_mock()
    guest = mock_guest(execute_mock)
    guest.environment = module.shared('testing_farm_request').environments_requested[environment_index]
    guest.environment.excluded_packages = ['dummy2']

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    if environment_index == 1:
        execute_mock.assert_has_calls([])
        return

    command_calls = [
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/repo1', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/repo2', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/repo3', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
    ]
    mock_command_init.assert_has_calls(command_calls)

    write_calls = [
        call('https://example.com/dummy1-1.0.1-1.x86_64.rpm https://example.com/dummy2-1.0.1-1.src.rpm https://example.com/dummy2-1.0.1-1.x86_64.rpm https://example.com/package-install-1.0.1-1.src.rpm https://example.com/package-install-1.0.2-1.src.rpm https://example.com/package-install-1.0.3-1.src.rpm https://example.com/package-install-1.0.3-1.x86_64.rpm'),
        call('https://example.com/dummy1-1.0.1-1.x86_64.rpm https://example.com/package-install-1.0.3-1.x86_64.rpm')
    ]

    mock_file.write.assert_has_calls(write_calls)
    assert mock_file.flush.called

    execute_calls = [
        call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)'),
        call('command -v dnf'),
        call('curl --output /etc/yum.repos.d/repo4.repo.repo -LO https://example.com/repo4.repo'),
        call('mkdir -pv dummy-path'),
        call('cd dummy-path; cat dummy.txt | xargs -n1 curl -sO'),
        *generate_createrepo_calls(repo_name='test-artifacts', repo_path='dummy-path'),
        call('dnf -y reinstall $(cat dummy.txt)'),
        call('dnf -y downgrade --allowerasing $(cat dummy.txt)'),
        call('dnf -y update --allowerasing $(cat dummy.txt)'),
        call('dnf -y install --allowerasing $(cat dummy.txt)'),
        call('cat dummy.txt | xargs basename --suffix=.rpm | xargs rpm -q')
    ]

    execute_mock.assert_has_calls(execute_calls)


def test_guest_setup_forced_artifacts(module, tmpdir, monkeypatch):
    mock_file = Bunch(name='dummy.txt', write=MagicMock(), flush=MagicMock())

    # must implement context protocol, I cannot get it to work with Bunch :/
    class MockNamedTemporaryFile(Bunch):
        # pylint: disable=too-few-public-methods
        def __enter__(self):
            return mock_file

        def __exit__(self, *args, **kwargs):
            pass

    mock_tempfile = MockNamedTemporaryFile

    monkeypatch.setattr(gluetool_modules_framework.helpers.install_repository, 'NamedTemporaryFile', mock_tempfile)
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def mock_run_output(stdout):
        return MagicMock(
            exit_code=0,
            stdout=stdout,
            stderr=''
        )

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(side_effect=[
        mock_run_output('\n'.join([
            'https://example.com/forced-1.x86_64.rpm',
        ])),
    ])

    monkeypatch.setattr(Command, '__init__', mock_command_init)
    monkeypatch.setattr(Command, 'run', mock_command_run)

    execute_mock = get_execute_mock()
    guest = mock_guest(execute_mock)
    guest.environment = module.shared('testing_farm_request').environments_requested[0]
    guest.environment.excluded_packages = ['dummy2']

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir), forced_artifacts=[Artifact(
        id='https://example.com/forced-repo', packages=None, type='repository')])

    command_calls = [
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/forced-repo', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
    ]
    mock_command_init.assert_has_calls(command_calls)

    write_calls = [
        call('https://example.com/forced-1.x86_64.rpm'),
        call('https://example.com/forced-1.x86_64.rpm')
    ]

    mock_file.write.assert_has_calls(write_calls)
    assert mock_file.flush.called

    execute_calls = [
        call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)'),
        call('command -v dnf'),
        call('mkdir -pv dummy-path'),
        call('cd dummy-path; cat dummy.txt | xargs -n1 curl -sO'),
        *generate_createrepo_calls(repo_name='test-artifacts', repo_path='dummy-path'),
        call('dnf -y reinstall $(cat dummy.txt)'),
        call('dnf -y downgrade --allowerasing $(cat dummy.txt)'),
        call('dnf -y update --allowerasing $(cat dummy.txt)'),
        call('dnf -y install --allowerasing $(cat dummy.txt)'),
        call('cat dummy.txt | xargs basename --suffix=.rpm | xargs rpm -q')
    ]

    execute_mock.assert_has_calls(execute_calls)


def test_packages_threshold(module, tmpdir, monkeypatch):
    module._config['packages-amount-threshold'] = 5
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def mock_run_output(stdout):
        return MagicMock(
            exit_code=0,
            stdout=stdout,
            stderr=''
        )

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(side_effect=[
        mock_run_output('\n'.join([
            'https://example.com/dummy1-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy2-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy3-1.0.1-1.src.rpm',
            'https://example.com/dummy4-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy5-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy6-1.0.1-1.src.rpm'
        ])),
    ])

    monkeypatch.setattr(Command, '__init__', mock_command_init)
    monkeypatch.setattr(Command, 'run', mock_command_run)

    execute_mock = get_execute_mock()
    guest = mock_guest(execute_mock)
    guest.environment = module.shared('testing_farm_request').environments_requested[0]

    with pytest.raises(GlueError, match='Too many packages to install: 6'):
        module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))


@pytest.mark.parametrize('environment_index', [0, 1], ids=['multiple-repositories', 'no-repositories'])
def test_guest_setup_bootc(module, environment_index, tmpdir, monkeypatch):
    mock_file = Bunch(name='dummy.txt', write=MagicMock(), flush=MagicMock())

    # must implement context protocol, I cannot get it to work with Bunch :/
    class MockNamedTemporaryFile(Bunch):
        # pylint: disable=too-few-public-methods
        def __enter__(self):
            return mock_file

        def __exit__(self, *args, **kwargs):
            pass

    mock_tempfile = MockNamedTemporaryFile

    monkeypatch.setattr(gluetool_modules_framework.helpers.install_repository, 'NamedTemporaryFile', mock_tempfile)
    module.execute()

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def mock_run_output(stdout):
        return MagicMock(
            exit_code=0,
            stdout=stdout,
            stderr=''
        )

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(side_effect=[
        # Artifact(id='https://example.com/repo1', packages=None, type='repository')
        mock_run_output(''),
        # Artifact(id='https://example.com/repo2', packages=None, type='repository')
        # dummy2 shout not be installed, as we will exclude it, but src.rpm should be downloaded
        mock_run_output('\n'.join([
            'https://example.com/dummy1-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy2-1.0.1-1.x86_64.rpm',
            'https://example.com/dummy2-1.0.1-1.src.rpm'
        ])),
        # Artifact(id='https://example.com/repo3', packages=['package-install-1'], type='repository')
        # * package-noinstall should be ignored completely, including src.rpm
        # * package-install only latest NVR should be installed - package-install-1.0.3.rpm
        mock_run_output('\n'.join([
            'https://example.com/package-install-1.0.1-1.x86_64.rpm',
            'https://example.com/package-install-1.0.1-1.src.rpm',
            'https://example.com/package-install-1.0.3-1.x86_64.rpm',
            'https://example.com/package-install-1.0.3-1.src.rpm',
            'https://example.com/package-install-1.0.2-1.x86_64.rpm',
            'https://example.com/package-install-1.0.2-1.src.rpm',
            'https://example.com/package-noinstall-1.0.1-1.x86_64.rpm',
            'https://example.com/package-noinstall-1.0.1-1.src.rpm'
        ])),
        mock_run_output('tmt output')
    ])

    monkeypatch.setattr(Command, '__init__', mock_command_init)
    monkeypatch.setattr(Command, 'run', mock_command_run)

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock)
    guest.environment = module.shared('testing_farm_request').environments_requested[environment_index]
    guest.environment.excluded_packages = ['dummy2']

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    if environment_index == 1:
        execute_mock.assert_has_calls([])
        return

    command_calls = [
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/repo1', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/repo2', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['dnf', 'repoquery', '-q', '--repofrompath=artifacts-repo,https://example.com/repo3', '--repo', 'artifacts-repo', '--location', '--disable-modular-filtering']),  # noqa
        call(['bash', '-c', 'tmt -vvv run provision --how connect --guest guest0 --key guest-key --port 22 prepare --how install --package=https://example.com/dummy1-1.0.1-1.x86_64.rpm --package=https://example.com/package-install-1.0.3-1.x86_64.rpm'])  # noqa
    ]
    mock_command_init.assert_has_calls(command_calls)

    mock_file.write.assert_not_called()

    execute_calls = [
        call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)'),
        call('command -v dnf'),
        call('curl --output /etc/yum.repos.d/repo4.repo.repo -LO https://example.com/repo4.repo'),
    ]

    execute_mock.assert_has_calls(execute_calls)
