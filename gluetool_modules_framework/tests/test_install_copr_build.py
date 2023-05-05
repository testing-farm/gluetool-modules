# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

# Copyright Contributors to te Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import gluetool
import os
import shutil
import copy
from mock import MagicMock
from mock import call
import gluetool_modules_framework.libs.sut_installation
from gluetool_modules_framework.helpers.install_copr_build import InstallCoprBuild
from gluetool_modules_framework.libs.sut_installation import SUTInstallationFailedError, INSTALL_COMMANDS_FILE
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage
from . import create_module, patch_shared, check_loadable

LOG_DIR_NAME = 'artifact-installation'


def mock_guest(execute_mock, artifacts=None):
    guest_mock = MagicMock()
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock
    if artifacts:
        guest_mock.environment.artifacts = artifacts

    return guest_mock


def assert_log_files(guest, log_dirpath, file_names=None):
    if not file_names:
        file_names = [
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Reinstall-packages.txt',
            '4-Install-packages.txt',
            '5-Verify-packages-installed.txt'
        ]

    installation_log_dir = os.path.join(
        log_dirpath,
        '{}-{}'.format(LOG_DIR_NAME, guest.name)
    )

    os.path.isdir(installation_log_dir)

    for file_name in file_names:
        filepath = os.path.join(installation_log_dir, file_name)
        if not os.path.isfile(filepath):
            assert False, 'File {} should exist'.format(filepath)


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(InstallCoprBuild)[1]

    module._config['log-dir-name'] = LOG_DIR_NAME
    module._config['download-path'] = 'some-download-path'

    return module


@pytest.fixture(name='module_shared_patched')
def fixture_module_shared_patched(module, monkeypatch):
    """
    Modification of the `InstallCoprBuild` module such as mocking shared functions.
    """
    primary_task_mock = MagicMock()
    primary_task_mock.repo_url = 'dummy_repo_url'
    primary_task_mock.rpm_urls = ['dummy_rpm_url1', 'dummy_rpm_url2']
    primary_task_mock.srpm_urls = ['dummy_srpm_url1', 'dummy_srpm_url2']
    primary_task_mock.rpm_names = ['dummy_rpm_names1', 'dummy_rpm_names2']
    primary_task_mock.project = 'copr/project'

    def tasks_mock(task_ids=None):
        """
        Mock of shared function `tasks`. Returns a list of `CoprTask` mocks.

        The parameter `task_ids` represents artifacts to be installed. If not specified, a single task will be returned.
        Otherwise, the single `primary_task_mock` will be copied over for each entry in `task_ids` and their properties
        will be modified so each `CoprTask` mocks is distinguishable from each other by the `task_id`.
        """
        if task_ids:
            tasks = []
            for task_id in task_ids:
                def append_task_id(s):
                    return '{}_{}'.format(s, task_id)

                tasks.append(copy.deepcopy(primary_task_mock))
                tasks[-1].repo_url = append_task_id(tasks[-1].repo_url)
                tasks[-1].rpm_urls[0] = append_task_id(tasks[-1].rpm_urls[0])
                tasks[-1].rpm_urls[1] = append_task_id(tasks[-1].rpm_urls[1])
                tasks[-1].srpm_urls[0] = append_task_id(tasks[-1].srpm_urls[0])
                tasks[-1].srpm_urls[1] = append_task_id(tasks[-1].srpm_urls[1])
                tasks[-1].rpm_names[0] = append_task_id(tasks[-1].rpm_names[0])
                tasks[-1].rpm_names[1] = append_task_id(tasks[-1].rpm_names[1])
                tasks[-1].project = append_task_id(tasks[-1].project)
        else:
            tasks = [primary_task_mock]

        return tasks

    patch_shared(monkeypatch, module, {
        'setup_guest': None
    }, callables={
        'tasks': tasks_mock
    })

    return module, primary_task_mock


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/install_copr_build.py', 'InstallCoprBuild')


@pytest.mark.parametrize('guest_artifacts, expected_commands, expected_filenames', [
    (
        # Test case no. 1
        None,  # No input artifacts
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project-1.repo',
            'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" dummy_rpm_url1 dummy_rpm_url2 dummy_srpm_url1 dummy_srpm_url2',  # noqa
            'dnf --allowerasing -y reinstall dummy_rpm_url1 || true',
            'dnf --allowerasing -y reinstall dummy_rpm_url2 || true',
            'dnf --allowerasing -y install dummy_rpm_url1 dummy_rpm_url2',
            'rpm -q dummy_rpm_names1',
            'rpm -q dummy_rpm_names2',
        ],
        None  # No expected generated files - use the default ones in `assert_log_files()`
    ),
    (
        # Test case no. 2
        [  # Input artifacts
            {'type': 'fedora-copr-build', 'id': 'artifact1'},
            {'type': 'ignore-this-type', 'id': 'artifact-other'},
            {'type': 'fedora-copr-build', 'id': 'artifact2'},
            {'type': 'ignore-this-type-too', 'id': 'artifact-other2'},
            {'type': 'fedora-copr-build', 'id': 'artifact3'}
        ],
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy_repo_url_artifact1 --retry 5 --output /etc/yum.repos.d/copr_build-copr_project_artifact1-1.repo',
            'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" dummy_rpm_url1_artifact1 dummy_rpm_url2_artifact1 dummy_srpm_url1_artifact1 dummy_srpm_url2_artifact1',
            'dnf --allowerasing -y reinstall dummy_rpm_url1_artifact1 || true',
            'dnf --allowerasing -y reinstall dummy_rpm_url2_artifact1 || true',
            'curl -v dummy_repo_url_artifact2 --retry 5 --output /etc/yum.repos.d/copr_build-copr_project_artifact2-2.repo',
            'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" dummy_rpm_url1_artifact2 dummy_rpm_url2_artifact2 dummy_srpm_url1_artifact2 dummy_srpm_url2_artifact2',
            'dnf --allowerasing -y reinstall dummy_rpm_url1_artifact2 || true',
            'dnf --allowerasing -y reinstall dummy_rpm_url2_artifact2 || true',
            'curl -v dummy_repo_url_artifact3 --retry 5 --output /etc/yum.repos.d/copr_build-copr_project_artifact3-3.repo',
            'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" dummy_rpm_url1_artifact3 dummy_rpm_url2_artifact3 dummy_srpm_url1_artifact3 dummy_srpm_url2_artifact3',
            'dnf --allowerasing -y reinstall dummy_rpm_url1_artifact3 || true',
            'dnf --allowerasing -y reinstall dummy_rpm_url2_artifact3 || true',
            'dnf --allowerasing -y install dummy_rpm_url1_artifact1 dummy_rpm_url2_artifact1 dummy_rpm_url1_artifact2 dummy_rpm_url2_artifact2 dummy_rpm_url1_artifact3 dummy_rpm_url2_artifact3',
            'rpm -q dummy_rpm_names1_artifact1',
            'rpm -q dummy_rpm_names2_artifact1',
            'rpm -q dummy_rpm_names1_artifact2',
            'rpm -q dummy_rpm_names2_artifact2',
            'rpm -q dummy_rpm_names1_artifact3',
            'rpm -q dummy_rpm_names2_artifact3',
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Reinstall-packages.txt',
            '4-Download-copr-repository.txt',
            '5-Download-rpms-from-copr.txt',
            '6-Reinstall-packages.txt',
            '7-Download-copr-repository.txt',
            '8-Download-rpms-from-copr.txt',
            '9-Reinstall-packages.txt',
            '10-Install-packages.txt',
            '11-Verify-packages-installed.txt',
            '12-Verify-packages-installed.txt',
            '13-Verify-packages-installed.txt',
        ]
    )
])
def test_setup_guest(module_shared_patched, tmpdir, guest_artifacts, expected_commands, expected_filenames):
    module, primary_task_mock = module_shared_patched

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock, artifacts=guest_artifacts)

    module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    calls = [call('command -v dnf')] * 2 + [call(c) for c in expected_commands]
    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == len(calls)
    assert_log_files(guest, str(tmpdir), file_names=expected_filenames)

    with open(os.path.join(str(tmpdir), 'artifact-installation-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == '\n'.join(expected_commands) + '\n'


def test_no_dnf(module_shared_patched, tmpdir):
    module, primary_task_mock = module_shared_patched

    def execute_mock_side_effect(cmd):
        if cmd == 'command -v dnf':
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        return MagicMock(stdout='', stderr='')

    execute_mock = MagicMock()
    execute_mock.side_effect = execute_mock_side_effect

    guest = mock_guest(execute_mock)
    module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    calls = [
        call('command -v dnf'),
        call('mkdir -pv some-download-path'),
        call('curl -v dummy_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project-1.repo'),
        call('cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" dummy_rpm_url1 dummy_rpm_url2 dummy_srpm_url1 dummy_srpm_url2'),  # noqa
        call('yum -y reinstall dummy_rpm_url1'),
        call('yum -y reinstall dummy_rpm_url2'),
        call('yum -y downgrade dummy_rpm_url1 dummy_rpm_url2'),
        call('yum -y install dummy_rpm_url1 dummy_rpm_url2'),
        call('rpm -q dummy_rpm_names1'),
        call('rpm -q dummy_rpm_names2')
    ]

    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == 11
    assert_log_files(guest, str(tmpdir), file_names=[
        '0-Create-artifacts-directory.txt',
        '1-Download-copr-repository.txt',
        '2-Download-rpms-from-copr.txt',
        '3-Reinstall-packages.txt',
        '4-Downgrade-packages.txt',
        '5-Install-packages.txt',
        '6-Verify-packages-installed.txt'
        ])


def test_nvr_check_fails(module_shared_patched, tmpdir):
    module, primary_task_mock = module_shared_patched

    def execute_mock(cmd):
        if cmd.startswith('rpm -q') or cmd.startswith('yum -y downgrade'):
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        return MagicMock(stdout='', stderr='')

    guest = mock_guest(execute_mock)

    ret = module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    assert ret.is_error

    outputs, exc = ret.value

    assert len(outputs) == 1
    assert outputs[0].stage == GuestSetupStage.ARTIFACT_INSTALLATION
    assert outputs[0].label == 'Copr build(s) installation'
    assert outputs[0].log_path == '{}/artifact-installation-guest0'.format(str(tmpdir))
    assert isinstance(outputs[0].additional_data, gluetool_modules_framework.libs.sut_installation.SUTInstallation)

    assert isinstance(exc, SUTInstallationFailedError)
    assert str(exc) == 'Test environment installation failed: Verify packages installed'

    assert_log_files(guest, str(tmpdir))


def test_repo_download_fails(module_shared_patched, tmpdir):
    module, primary_task_mock = module_shared_patched

    def execute_mock(cmd):
        if cmd.startswith('curl'):
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        return MagicMock(stdout='', stderr='')

    guest = mock_guest(execute_mock)

    ret = module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    assert ret.is_error

    outputs, exc = ret.value

    assert len(outputs) == 1
    assert outputs[0].stage == GuestSetupStage.ARTIFACT_INSTALLATION
    assert outputs[0].label == 'Copr build(s) installation'
    assert outputs[0].log_path == '{}/artifact-installation-guest0'.format(str(tmpdir))
    assert isinstance(outputs[0].additional_data, gluetool_modules_framework.libs.sut_installation.SUTInstallation)

    assert isinstance(exc, SUTInstallationFailedError)
    assert str(exc) == 'Test environment installation failed: Download copr repository'

    assert_log_files(guest, str(tmpdir), file_names=['1-Download-copr-repository.txt'])
