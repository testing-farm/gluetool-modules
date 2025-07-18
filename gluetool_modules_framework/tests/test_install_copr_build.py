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
from gluetool_modules_framework.tests.test_libs_repo import generate_calls as generate_createrepo_calls
from gluetool_modules_framework.tests.test_libs_repo import generate_cmds as generate_createrepo_cmds
import gluetool_modules_framework.libs.sut_installation
from gluetool_modules_framework.helpers.install_copr_build import InstallCoprBuild
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage
from gluetool_modules_framework.libs.sut_installation import SUTInstallationFailedError, INSTALL_COMMANDS_FILE
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact
from . import create_module, patch_shared, check_loadable

LOG_DIR_NAME = 'artifact-installation'


def mock_guest(execute_mock, artifacts=None, environment=None):
    guest_mock = MagicMock()
    guest_mock.hostname = 'guest0'
    guest_mock.key = 'guest-key'
    guest_mock.port = 22
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock
    guest_mock.environment = environment or TestingEnvironment()
    if artifacts:
        guest_mock.environment.artifacts = artifacts

    return guest_mock


def get_execute_mock():
    def side_effect(cmd):
        if 'bootc' in cmd:
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        else:
            return MagicMock(stdout='', stderr='')

    return MagicMock(side_effect=side_effect)


def assert_log_files(guest, log_dirpath, file_names=None):
    if not file_names:
        file_names = [
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Reinstall-packages.txt',
            '6-Install-packages.txt',
            '7-Verify-packages-installed.txt'
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
    primary_task_mock.repo_url = 'dummyX_repo_url'
    primary_task_mock.rpm_urls = ['https://example.com/dummyX_rpm_name1-1.0.1-el7.rpm', 'https://example.com/dummyX_rpm_name2-1.0.1-el7.rpm']  # noqa
    primary_task_mock.srpm_urls = ['https://example.com/dummyX_rpm_name1-1.0.1-el7.src.rpm', 'https://example.com/dummyX_rpm_name2-1.0.1-el7.src.rpm']  # noqa
    primary_task_mock.rpm_names = ['dummyX_rpm_name1', 'dummyX_rpm_name2']
    primary_task_mock.project = 'copr/projectX'

    def tasks_mock(task_ids=None):
        """
        Mock of shared function `tasks`. Returns a list of `CoprTask` mocks.

        The parameter `task_ids` represents artifacts to be installed. If not specified, a single task will be returned.
        Otherwise, the single `primary_task_mock` will be copied over for each entry in `task_ids` and their properties
        will be modified so each `CoprTask` mocks is distinguishable from each other by the `task_id`.
        """
        if task_ids:
            tasks = []
            for number, _ in enumerate(task_ids, 1):
                def generate_id(item: str):
                    return item.replace('X', str(number))

                tasks.append(copy.deepcopy(primary_task_mock))
                tasks[-1].repo_url = generate_id(tasks[-1].repo_url)
                tasks[-1].rpm_urls[0] = generate_id(tasks[-1].rpm_urls[0])
                tasks[-1].rpm_urls[1] = generate_id(tasks[-1].rpm_urls[1])
                tasks[-1].srpm_urls[0] = generate_id(tasks[-1].srpm_urls[0])
                tasks[-1].srpm_urls[1] = generate_id(tasks[-1].srpm_urls[1])
                tasks[-1].rpm_names[0] = generate_id(tasks[-1].rpm_names[0])
                tasks[-1].rpm_names[1] = generate_id(tasks[-1].rpm_names[1])
                tasks[-1].project = generate_id(tasks[-1].project)
        else:
            tasks = [primary_task_mock]

        return tasks

    patch_shared(monkeypatch, module, {
        'setup_guest': None
    }, callables={
        'tasks': tasks_mock,
        'tmt_command': lambda: ['tmt']
    })

    return module, primary_task_mock


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/install_copr_build.py', 'InstallCoprBuild')


@pytest.mark.parametrize('guest_artifacts, guest_environment, expected_commands, expected_filenames, forced_artifacts', [
    (
        #
        # Test case - single artifact
        #
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
        ],
        None,
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm || true',
            'dnf -y install --allowerasing https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm',
            'rpm -q dummy1_rpm_name1',
            'rpm -q dummy1_rpm_name2',
        ],
        None,  # No expected generated files - use the default ones in `assert_log_files()`
        None
    ),
    #
    # Test case - multiple artifacts
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
            Artifact(type='ignore-this-type', id='artifact-other'),
            Artifact(type='fedora-copr-build', id='artifact2'),
            Artifact(type='ignore-this-type-too', id='artifact-other2'),
            Artifact(type='fedora-copr-build', id='artifact3'),
        ],
        None,
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm || true',
            'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm '
                'https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm || true',
            'curl -v dummy3_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project3-3.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.src.rpm '
                'https://example.com/dummy3_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm || true',
            (
                'dnf -y install --allowerasing '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm'
            ),
            'rpm -q dummy1_rpm_name1',
            'rpm -q dummy1_rpm_name2',
            'rpm -q dummy2_rpm_name1',
            'rpm -q dummy2_rpm_name2',
            'rpm -q dummy3_rpm_name1',
            'rpm -q dummy3_rpm_name2',
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Reinstall-packages.txt',
            '6-Download-copr-repository.txt',
            '7-Download-rpms-from-copr.txt',
            '8-Create-repository.txt',
            '9-Add-repository.txt',
            '10-Reinstall-packages.txt',
            '11-Download-copr-repository.txt',
            '12-Download-rpms-from-copr.txt',
            '13-Create-repository.txt',
            '14-Add-repository.txt',
            '15-Reinstall-packages.txt',
            '16-Install-packages.txt',
            '17-Verify-packages-installed.txt',
            '18-Verify-packages-installed.txt',
            '19-Verify-packages-installed.txt',
        ],
        None
    ),
    #
    # Test case - with-excludes
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
            Artifact(type='ignore-this-type', id='artifact-other'),
            Artifact(type='fedora-copr-build', id='artifact2'),
            Artifact(type='ignore-this-type-too', id='artifact-other2'),
            Artifact(type='fedora-copr-build', id='artifact3'),
        ],
        TestingEnvironment(
            excluded_packages=['dummy1_rpm_name1', 'dummy1_rpm_name2', 'dummy2_rpm_name1', 'dummy2_rpm_name2']
        ),
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'curl -v dummy3_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project3-3.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm || true',
            (
                'dnf -y install --allowerasing '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm'
            ),
            'rpm -q dummy3_rpm_name1',
            'rpm -q dummy3_rpm_name2',
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Download-copr-repository.txt',
            '6-Download-rpms-from-copr.txt',
            '7-Create-repository.txt',
            '8-Add-repository.txt',
            '9-Download-copr-repository.txt',
            '10-Download-rpms-from-copr.txt',
            '11-Create-repository.txt',
            '12-Add-repository.txt',
            '13-Reinstall-packages.txt',
            '14-Install-packages.txt',
            '15-Verify-packages-installed.txt'
        ],
        None
    ),
    #
    # Test case - all-excluded
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
            Artifact(type='fedora-copr-build', id='artifact2'),
        ],
        TestingEnvironment(
            excluded_packages=['dummy1_rpm_name1', 'dummy1_rpm_name2', 'dummy2_rpm_name1', 'dummy2_rpm_name2']
        ),
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path')
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Download-copr-repository.txt',
            '6-Download-rpms-from-copr.txt',
            '7-Create-repository.txt',
            '8-Add-repository.txt'
        ],
        None
    ),
    #
    # Test case - with "install=False"
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1', install=False),
            Artifact(type='fedora-copr-build', id='artifact2'),
        ],
        None,
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm || true',
            'dnf -y install --allowerasing https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm',
            'rpm -q dummy2_rpm_name1',
            'rpm -q dummy2_rpm_name2',
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Download-copr-repository.txt',
            '6-Download-rpms-from-copr.txt',
            '7-Create-repository.txt',
            '8-Add-repository.txt',
            '9-Reinstall-packages.txt',
            '10-Install-packages.txt',
            '11-Verify-packages-installed.txt'
        ],
        None
    ),
    (
        #
        # Test case - forced artifact
        #
        [  # This artifacts should be ignored
            Artifact(type='fedora-copr-build', id='ignored-artifact'),
            Artifact(type='fedora-copr-build', id='ignored-artifact'),
            Artifact(type='fedora-copr-build', id='ignored-artifact'),
        ],
        None,
        [  # Expected install commands
            'mkdir -pv some-download-path',
            'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo',
            (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm '
                'https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
            ),
            *generate_createrepo_cmds(repo_name='test-artifacts', repo_path='some-download-path'),
            'dnf -y reinstall https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm || true',
            'dnf -y reinstall https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm || true',
            'dnf -y install --allowerasing https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm',
            'rpm -q dummy1_rpm_name1',
            'rpm -q dummy1_rpm_name2',
        ],
        None,  # No expected generated files - use the default ones in `assert_log_files()`
        [Artifact(type='fedora-copr-build', id='artifact1')]  # This artifact should be installed
    ),

], ids=['single-artifact', 'multiple-artifacts', 'with-excludes', 'all-excluded', 'with-install-false', 'forced-artifact'])
def test_setup_guest(module_shared_patched, tmpdir, guest_artifacts, guest_environment, expected_commands, expected_filenames, forced_artifacts):

    module, primary_task_mock = module_shared_patched

    execute_mock = get_execute_mock()
    guest = mock_guest(execute_mock, artifacts=guest_artifacts, environment=guest_environment)

    module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION,
                       log_dirpath=str(tmpdir), forced_artifacts=forced_artifacts)

    calls = (
        [call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)')] +
        [call('command -v dnf')] * 2 +
        [call(c) for c in expected_commands]
    )
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
        elif 'bootc' in cmd:
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))

        return MagicMock(stdout='', stderr='')

    execute_mock = MagicMock()
    execute_mock.side_effect = execute_mock_side_effect

    guest = mock_guest(execute_mock, [Artifact(id='dummy-artifact', type='fedora-copr-build')])
    module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION, log_dirpath=str(tmpdir))

    calls = [
        call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)'),
        call('command -v dnf'),
        call('command -v dnf'),
        call('mkdir -pv some-download-path'),
        call('curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'),
        call(
            'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
            'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
            'https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
            'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm '
            'https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm'
        ),
        *generate_createrepo_calls(repo_name='test-artifacts', repo_path='some-download-path'),
        call('yum -y reinstall https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm'),
        call('yum -y reinstall https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm'),
        call('yum -y downgrade https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm'),
        call('yum -y install https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm'),
        call('rpm -q dummy1_rpm_name1'),
        call('rpm -q dummy1_rpm_name2')
    ]

    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == len(calls)
    assert_log_files(guest, str(tmpdir), file_names=[
        '0-Create-artifacts-directory.txt',
        '1-Download-copr-repository.txt',
        '2-Download-rpms-from-copr.txt',
        '3-Create-repository.txt',
        '4-Add-repository.txt',
        '5-Reinstall-packages.txt',
        '6-Downgrade-packages.txt',
        '7-Install-packages.txt',
        '8-Verify-packages-installed.txt'
        ])


def test_nvr_check_fails(module_shared_patched, tmpdir):
    module, primary_task_mock = module_shared_patched

    def execute_mock(cmd):
        if cmd.startswith('rpm -q') or cmd.startswith('yum -y downgrade'):
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        elif 'bootc' in cmd:
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))

        return MagicMock(stdout='', stderr='')

    guest = mock_guest(execute_mock, [Artifact(id='dummy-artifact', type='fedora-copr-build')])

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
        elif 'bootc' in cmd:
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))

        return MagicMock(stdout='', stderr='')

    guest = mock_guest(execute_mock, [Artifact(id='dummy-artifact', type='fedora-copr-build')])

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


@pytest.mark.parametrize('guest_artifacts, guest_environment, expected_commands, expected_filenames, forced_artifacts', [
    (
        #
        # Test case - single artifact
        #
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
        ],
        None,
        [  # Expected install commands
            ['bash', '-c', 'mkdir -pv some-download-path'],
            ['bash', '-c', 'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', (
                'tmt -vvv run provision --how connect --guest guest0 --key guest-key --port 22 prepare --how install '
                '--package=https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm')]
        ],
        None,  # No expected generated files - use the default ones in `assert_log_files()`
        None
    ),
    #
    # Test case - multiple artifacts
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
            Artifact(type='ignore-this-type', id='artifact-other'),
            Artifact(type='fedora-copr-build', id='artifact2'),
            Artifact(type='ignore-this-type-too', id='artifact-other2'),
            Artifact(type='fedora-copr-build', id='artifact3'),
        ],
        None,
        [  # Expected install commands
            ['bash', '-c', 'mkdir -pv some-download-path'],
            ['bash', '-c', 'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', 'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', 'curl -v dummy3_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project3-3.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', (
                'tmt -vvv run provision --how connect --guest guest0 --key guest-key --port 22 prepare --how install '
                '--package=https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                '--package=https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                '--package=https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm')]
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Reinstall-packages.txt',
            '6-Download-copr-repository.txt',
            '7-Download-rpms-from-copr.txt',
            '8-Create-repository.txt',
            '9-Add-repository.txt',
            '10-Reinstall-packages.txt',
            '11-Download-copr-repository.txt',
            '12-Download-rpms-from-copr.txt',
            '13-Create-repository.txt',
            '14-Add-repository.txt',
            '15-Reinstall-packages.txt',
            '16-Install-packages.txt',
            '17-Verify-packages-installed.txt',
            '18-Verify-packages-installed.txt',
            '19-Verify-packages-installed.txt',
        ],
        None
    ),
    #
    # Test case - with-excludes
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
            Artifact(type='ignore-this-type', id='artifact-other'),
            Artifact(type='fedora-copr-build', id='artifact2'),
            Artifact(type='ignore-this-type-too', id='artifact-other2'),
            Artifact(type='fedora-copr-build', id='artifact3'),
        ],
        TestingEnvironment(
            excluded_packages=['dummy1_rpm_name1', 'dummy1_rpm_name2', 'dummy2_rpm_name1', 'dummy2_rpm_name2']
        ),
        [  # Expected install commands
            ['bash', '-c', 'mkdir -pv some-download-path'],
            ['bash', '-c', 'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', 'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', 'curl -v dummy3_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project3-3.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy3_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy3_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', (
                'tmt -vvv run provision --how connect --guest guest0 --key guest-key --port 22 prepare --how install '
                '--package=https://example.com/dummy3_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy3_rpm_name2-1.0.1-el7.rpm')]
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Download-copr-repository.txt',
            '6-Download-rpms-from-copr.txt',
            '7-Create-repository.txt',
            '8-Add-repository.txt',
            '9-Download-copr-repository.txt',
            '10-Download-rpms-from-copr.txt',
            '11-Create-repository.txt',
            '12-Add-repository.txt',
            '13-Reinstall-packages.txt',
            '14-Install-packages.txt',
            '15-Verify-packages-installed.txt'
        ],
        None
    ),
    #
    # Test case - all-excluded
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1'),
            Artifact(type='fedora-copr-build', id='artifact2'),
        ],
        TestingEnvironment(
            excluded_packages=['dummy1_rpm_name1', 'dummy1_rpm_name2', 'dummy2_rpm_name1', 'dummy2_rpm_name2']
        ),
        [  # Expected install commands
            ['bash', '-c', 'mkdir -pv some-download-path'],
            ['bash', '-c', 'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', 'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm')],
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Download-copr-repository.txt',
            '6-Download-rpms-from-copr.txt',
            '7-Create-repository.txt',
            '8-Add-repository.txt'
        ],
        None
    ),
    #
    # Test case - with "install=False"
    #
    (
        [  # Input artifacts
            Artifact(type='fedora-copr-build', id='artifact1', install=False),
            Artifact(type='fedora-copr-build', id='artifact2'),
        ],
        None,
        [  # Expected install commands
            ['bash', '-c', 'mkdir -pv some-download-path'],
            ['bash', '-c', 'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', 'curl -v dummy2_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project2-2.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy2_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy2_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', (
                'tmt -vvv run provision --how connect --guest guest0 --key guest-key --port 22 prepare --how install '
                '--package=https://example.com/dummy2_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy2_rpm_name2-1.0.1-el7.rpm')]
        ],
        [  # Expected generated files
            '0-Create-artifacts-directory.txt',
            '1-Download-copr-repository.txt',
            '2-Download-rpms-from-copr.txt',
            '3-Create-repository.txt',
            '4-Add-repository.txt',
            '5-Download-copr-repository.txt',
            '6-Download-rpms-from-copr.txt',
            '7-Create-repository.txt',
            '8-Add-repository.txt',
            '9-Reinstall-packages.txt',
            '10-Install-packages.txt',
            '11-Verify-packages-installed.txt'
        ],
        None
    ),
    (
        #
        # Test case - forced artifact
        #
        [  # This artifacts should be ignored
            Artifact(type='fedora-copr-build', id='ignored-artifact'),
            Artifact(type='fedora-copr-build', id='ignored-artifact'),
            Artifact(type='fedora-copr-build', id='ignored-artifact'),
        ],
        None,
        [  # Expected install commands
            ['bash', '-c', 'mkdir -pv some-download-path'],
            ['bash', '-c', 'curl -v dummy1_repo_url --retry 5 --output /etc/yum.repos.d/copr_build-copr_project1-1.repo'],
            ['bash', '-c', (
                'cd some-download-path && curl -sL --retry 5 --remote-name-all -w "Downloaded: %{url_effective}\\n" '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm '
                'https://example.com/dummy1_rpm_name1-1.0.1-el7.src.rpm https://example.com/dummy1_rpm_name2-1.0.1-el7.src.rpm')],
            ['bash', '-c', (
                'tmt -vvv run provision --how connect --guest guest0 --key guest-key --port 22 prepare --how install '
                '--package=https://example.com/dummy1_rpm_name1-1.0.1-el7.rpm '
                '--package=https://example.com/dummy1_rpm_name2-1.0.1-el7.rpm')],
        ],
        None,  # No expected generated files - use the default ones in `assert_log_files()`
        [Artifact(type='fedora-copr-build', id='artifact1')]  # This artifact should be installed
    ),

], ids=['single-artifact', 'multiple-artifacts', 'with-excludes', 'all-excluded', 'with-install-false', 'forced-artifact'])
def test_setup_guest_bootc(module_shared_patched, tmpdir, guest_artifacts, guest_environment, expected_commands, expected_filenames, forced_artifacts, monkeypatch):

    module, primary_task_mock = module_shared_patched

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock, artifacts=guest_artifacts, environment=guest_environment)

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=MagicMock(stdout='test\ntest2', stderr='', exit_code=0))

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION,
                       log_dirpath=str(tmpdir), forced_artifacts=forced_artifacts)

    calls = [call(c) for c in expected_commands]
    mock_command_init.assert_has_calls(calls, any_order=False)
    assert mock_command_init.call_count == len(calls)
