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
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact
import gluetool_modules_framework.helpers.rules_engine

from . import create_module, patch_shared


def mock_guest(execute_mock, artifacts=None, environment=None):
    guest_mock = MagicMock()
    guest_mock.hostname = 'guest0'
    guest_mock.key = 'guest-key'
    guest_mock.port = 22
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock
    guest_mock.environment = environment or TestingEnvironment()
    guest_mock.environment.arch = 'x86_64'
    guest_mock.environment.artifacts = [
        Artifact(
            id='123123123',
            packages=None,
            type='fedora-koji-build'
        ),
        Artifact(
            id='123123124',
            packages=None,
            type='redhat-brew-build'
        ),
        Artifact(
            id='wrongid',
            packages=None,
            type='wongtype'
        ),
    ]
    if artifacts:
        guest_mock.environment.artifacts += artifacts

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
    module = create_module(gluetool_modules_framework.helpers.install_koji_build_execute.InstallKojiBuildExecute)[1]

    module._config['log-dir-name'] = 'log-dir-example'
    module._config['download-i686-builds'] = True

    def evaluate_instructions_mock(workarounds, callbacks):
        callbacks['steps']('instructions', 'commands', workarounds, 'context')

    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: MagicMock(),
        'evaluate_instructions': evaluate_instructions_mock,
        'setup_guest': None,
        'tmt_command': lambda: ['tmt']
    })

    return module


@pytest.fixture(name='local_guest')
def fixture_local_guest(module):
    guest = guest_module.NetworkedGuest(module, '127.0.0.1', key=MagicMock())
    guest.execute = get_execute_mock()
    guest.environment = TestingEnvironment(
        arch='x86_64',
        compose='dummy-compose'
    )

    return guest


def test_sanity_shared(module):
    assert module.glue.has_shared('setup_guest') is True


def test_setup_guest(module, local_guest):
    pass


def test_extract_artifacts(module, monkeypatch):
    assert module._extract_artifacts(mock_guest(MagicMock(return_value=MagicMock(stdout='', stderr='')))) == [
        Artifact(
            id='123123123',
            packages=None,
            type='fedora-koji-build'
        ),
        Artifact(
            id='123123124',
            packages=None,
            type='redhat-brew-build'
        ),
    ]


@pytest.mark.parametrize('guest_artifacts, expected_commands, forced_artifacts', [
    #
    # Test case 1 - install all artifacts
    #
    (
        None,  # No additional input artifacts, see `mock_guest` function for the base artifacts
        [  # Expected install commands
            '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123123',  # noqa
            '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123124 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123124',  # noqa
            'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | egrep -v "i686" | awk "{print \\$2}" | tee rpms-list',  # noqa
            'dnf -y reinstall $(cat rpms-list) || true',
            r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then dnf -y install --allowerasing $(cat rpms-list);else echo "Nothing to install, rpms-list is empty"; fi""",
            r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\n' | xargs -d'\n' rpm -q;else echo 'Nothing to verify, rpms-list is empty'; fi""",
        ],
        None
    ),
    #
    # Test case 2 - skip installation of some artifacts
    #
    (
        [  # Additional input artifacts, see `mock_guest` function for the base artifacts
            Artifact(id='skip-installing-me-1', packages=None, type='fedora-koji-build', install=False),
            Artifact(id='skip-installing-me-2', packages=None, type='redhat-brew-build', install=False),
        ],
        [  # Expected install commands
            '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123123',  # noqa
            '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123124 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123124',  # noqa
            '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-1 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-1 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-skip-installing-me-1',  # noqa
            '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-2 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-2 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-skip-installing-me-2',  # noqa
            'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | grep -Fv "$(cat rpms-list-skip-installing-me-1)" | grep -Fv "$(cat rpms-list-skip-installing-me-2)" | egrep -v "i686" | awk "{print \\$2}" | tee rpms-list',  # noqa
            'dnf -y reinstall $(cat rpms-list) || true',
            r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then dnf -y install --allowerasing $(cat rpms-list);else echo "Nothing to install, rpms-list is empty"; fi""",
            r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\n' | xargs -d'\n' rpm -q;else echo 'Nothing to verify, rpms-list is empty'; fi""",
        ],
        None
    ),
    #
    # Test case 3 - install forced artifact
    #
    (
        None,  # No additional input artifacts, see `mock_guest` function for the base artifacts
        [  # Expected install commands
            '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src forced-artifact || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src forced-artifact ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-forced-artifact',  # noqa
            'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | egrep -v "i686" | awk "{print \\$2}" | tee rpms-list',  # noqa
            'dnf -y reinstall $(cat rpms-list) || true',
            r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then dnf -y install --allowerasing $(cat rpms-list);else echo "Nothing to install, rpms-list is empty"; fi""",
            r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\n' | xargs -d'\n' rpm -q;else echo 'Nothing to verify, rpms-list is empty'; fi""",
        ],
        [Artifact(id='forced-artifact', packages=None, type='fedora-koji-build')],
    ),
])
def test_guest_setup(module, local_guest, tmpdir, guest_artifacts, expected_commands, forced_artifacts):
    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    execute_mock = get_execute_mock()
    guest = mock_guest(execute_mock, artifacts=guest_artifacts)

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir), forced_artifacts=forced_artifacts)

    calls = (
        [call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)')] +
        [call('command -v dnf')] * 2 +
        [call(c) for c in expected_commands]
    )
    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == len(calls)

    with open(os.path.join(str(tmpdir), 'log-dir-example-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == '\n'.join(expected_commands) + '\n'


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

    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    execute_mock = get_execute_mock()
    guest = mock_guest(execute_mock, artifacts=[
        Artifact(type='fedora-copr-build', id='artifact1'),
    ])

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))
    copr_module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    koji_commands = [
        '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123123',  # noqa
        '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123124 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123124',  # noqa
        'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | egrep -v "i686" | awk "{print \\$2}" | tee rpms-list',  # noqa
        'dnf -y reinstall $(cat rpms-list) || true',
        r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then dnf -y install --allowerasing $(cat rpms-list);else echo "Nothing to install, rpms-list is empty"; fi""",
        r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\n' | xargs -d'\n' rpm -q;else echo 'Nothing to verify, rpms-list is empty'; fi""",
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
    calls = (
        [call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)')] +
        [call('command -v dnf')] * 2 +
        [call(c) for c in koji_commands]
    )
    calls += (
        [call('type bootc && sudo bootc status && ((sudo bootc status --format yaml | grep -e "booted: null" -e "image: null") && exit 1 || exit 0)')] +
        [call('command -v dnf')] * 2 +
        [call(c) for c in copr_commands]
    )
    execute_mock.assert_has_calls(calls, any_order=False)
    assert execute_mock.call_count == len(calls)

    with open(os.path.join(str(tmpdir), 'log-dir-example-guest0', INSTALL_COMMANDS_FILE)) as f:
        assert f.read() == '\n'.join(koji_commands + copr_commands) + '\n'


def test_guest_setup_yum(module, local_guest, tmpdir):
    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    def execute_mock_side_effect(cmd):
        if cmd == 'command -v dnf':
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        elif 'bootc' in cmd:
            raise gluetool.glue.GlueCommandError('dummy_error', MagicMock(exit_code=1, stdout='', stderr=''))
        return MagicMock(stdout='', stderr='')

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    execute_mock.side_effect = execute_mock_side_effect
    guest = mock_guest(execute_mock)
    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir))

    calls = [
        call('command -v dnf'),
        call('command -v dnf'),
        call('( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123123'),  # noqa
        call('( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123124 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123124'),  # noqa
        call('ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | egrep -v "i686" | awk "{print \\$2}" | tee rpms-list'),  # noqa
        call('yum -y reinstall $(cat rpms-list)'),
        call('yum -y downgrade $(cat rpms-list)'),
        call('yum -y install $(cat rpms-list)'),
        call(r"""if [ ! -z "$(sed 's/\s//g' rpms-list)" ];then sed 's/.rpm$//' rpms-list | xargs -n1 command printf '%q\n' | xargs -d'\n' rpm -q;else echo 'Nothing to verify, rpms-list is empty'; fi""")
    ]

    execute_mock.assert_has_calls(calls)


@pytest.mark.parametrize('guest_artifacts, expected_commands, forced_artifacts', [
    #
    # Test case 1 - install all artifacts
    #
    (
        None,  # No additional input artifacts, see `mock_guest` function for the base artifacts
        [  # Expected install commands
            ['bash', '-c', '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123123'],  # noqa
            ['bash', '-c', '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123124 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123124'],  # noqa
            ['bash', '-c', 'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | egrep -v "i686" | awk "{print \\$2}" | xargs realpath | tee rpms-list'],  # noqa
            ['tmt', '-vvv', 'run', 'provision', '--how', 'connect', '--guest', 'guest0', '--key', 'guest-key', '--port', '22', 'prepare', '--how', 'install', '--package=test', '--package=test2'],  # noqa
        ],
        None
    ),
    #
    # Test case 2 - skip installation of some artifacts
    #
    (
        [  # Additional input artifacts, see `mock_guest` function for the base artifacts
            Artifact(id='skip-installing-me-1', packages=None, type='fedora-koji-build', install=False),
            Artifact(id='skip-installing-me-2', packages=None, type='redhat-brew-build', install=False),
        ],
        [  # Expected install commands
            ['bash', '-c', '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123123 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123123 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123123'],  # noqa
            ['bash', '-c', '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src 123123124 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src 123123124 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-123123124'],  # noqa
            ['bash', '-c', '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-1 || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-1 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-skip-installing-me-1'],  # noqa
            ['bash', '-c', '( brew download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-2 || brew download-task --arch noarch --arch x86_64 --arch i686 --arch src skip-installing-me-2 ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-skip-installing-me-2'],  # noqa
            ['bash', '-c', 'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | grep -Fv "$(cat rpms-list-skip-installing-me-1)" | grep -Fv "$(cat rpms-list-skip-installing-me-2)" | egrep -v "i686" | awk "{print \\$2}" | xargs realpath | tee rpms-list'],  # noqa
            ['tmt', '-vvv', 'run', 'provision', '--how', 'connect', '--guest', 'guest0', '--key', 'guest-key', '--port', '22', 'prepare', '--how', 'install', '--package=test', '--package=test2'],  # noqa
        ],
        None
    ),
    #
    # Test case 3 - install forced artifact
    #
    (
        None,  # No additional input artifacts, see `mock_guest` function for the base artifacts
        [  # Expected install commands
            ['bash', '-c', '( koji download-build --debuginfo --task-id --arch noarch --arch x86_64 --arch i686 --arch src forced-artifact || koji download-task --arch noarch --arch x86_64 --arch i686 --arch src forced-artifact ) | egrep Downloading | cut -d " " -f 3 | tee rpms-list-forced-artifact'],  # noqa
            ['bash', '-c', 'ls *[^.src].rpm | sed -r "s/(.*)-.*-.*/\\1 \\0/" | egrep -v "i686" | awk "{print \\$2}" | xargs realpath | tee rpms-list'],  # noqa
            ['tmt', '-vvv', 'run', 'provision', '--how', 'connect', '--guest', 'guest0', '--key', 'guest-key', '--port', '22', 'prepare', '--how', 'install', '--package=test', '--package=test2']
        ],
        [Artifact(id='forced-artifact', packages=None, type='fedora-koji-build')],
    ),
])
def test_guest_setup_bootc(module, local_guest, tmpdir, guest_artifacts, expected_commands, forced_artifacts, monkeypatch):
    stage = gluetool_modules_framework.libs.guest_setup.GuestSetupStage.ARTIFACT_INSTALLATION

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock, artifacts=guest_artifacts)

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=MagicMock(stdout='test\ntest2', stderr='', exit_code=0))

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    module.setup_guest(guest, stage=stage, log_dirpath=str(tmpdir), forced_artifacts=forced_artifacts)

    calls = [call(c, logger=guest.logger) for c in expected_commands]
    mock_command_init.assert_has_calls(calls, any_order=False)
    assert mock_command_init.call_count == len(calls)
