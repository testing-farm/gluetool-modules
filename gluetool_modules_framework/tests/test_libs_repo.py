# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import shlex
import urllib.parse
from typing import Any, Callable, List, cast
from unittest.mock import _Call, MagicMock, Mock, call

import gluetool
import gluetool_modules_framework.libs.repo
from gluetool_modules_framework.libs.guest import Guest
from gluetool_modules_framework.libs.sut_installation import SUTInstallation


def generate_cmds(repo_path: str = '/var/share/test-artifacts', repo_name: str = 'test-artifacts', pkglist: str = 'pkglist') -> List[str]:
    pkglist = f'{repo_path}/{pkglist}'
    return [
        (
            f'yum install -y createrepo && cd {shlex.quote(repo_path)} && '
            f'touch {shlex.quote(pkglist)} && createrepo --pkglist {shlex.quote(pkglist)} .'
        ),
        f'echo -e "[{repo_name}]\nname={repo_name}\ndescription=Test artifacts repository\nbaseurl=file://{urllib.parse.quote(repo_path)}\npriority=1\nenabled=1\ngpgcheck=0\n\n" > /etc/yum.repos.d/{repo_name}.repo'
    ]


def generate_calls(*args: Any, **kwargs: Any) -> List[_Call]:
    return [call(cmd) for cmd in generate_cmds(*args, **kwargs)]


def generate_translated_cmds(repo_path: str = '/var/share/test-artifacts', repo_name: str = 'test-artifacts', pkglist: str = 'pkglist') -> List[str]:
    # The functions `generate_cmds` and `generate_calls` are used in other tests.
    # However, they are not called via `SUTInstallation.run`, which will change yum commands
    # to dnf commands if dnf is available.
    pkglist = f'{repo_path}/{pkglist}'
    return [
        (
            f'dnf install --allowerasing -y createrepo && cd {shlex.quote(repo_path)} && '
            f'touch {shlex.quote(pkglist)} && createrepo --pkglist {shlex.quote(pkglist)} .'
        ),
        f'echo -e "[{repo_name}]\nname={repo_name}\ndescription=Test artifacts repository\nbaseurl=file://{urllib.parse.quote(repo_path)}\npriority=1\nenabled=1\ngpgcheck=0\n\n" > /etc/yum.repos.d/{repo_name}.repo'
    ]


def generate_translated_calls(*args: Any, **kwargs: Any) -> List[_Call]:
    # See comment for generate_translated_cmds.
    return [call(cmd) for cmd in generate_translated_cmds(*args, **kwargs)]


@pytest.fixture(name='mock_execute')
def fixture_mock_execute() -> Mock:
    return MagicMock(return_value=cast(gluetool.utils.ProcessOutput, MagicMock(stdout='', stderr='')))


@pytest.fixture(name='guest')
def fixture_guest(mock_execute: Callable) -> Guest:
    return cast(Guest, MagicMock(name='guest0', execute=mock_execute, logger=cast(gluetool.log.ContextAdapter, MagicMock())))


@pytest.fixture(name='sut_installation')
def fixture(guest: Guest) -> SUTInstallation:
    return SUTInstallation(
        cast(gluetool.Module, MagicMock(spec=gluetool.Module)),
        '/tmp/dummy',
        None
    )


def test_create_repo(guest, mock_execute, sut_installation):
    gluetool_modules_framework.libs.repo.create_repo(
        sut_installation,
        'dummy-repo',
        'some/path/to a/repo'
    )

    sut_installation.run(guest)

    assert mock_execute.call_count == 3
    mock_execute.assert_has_calls(generate_translated_calls(repo_path='some/path/to a/repo', repo_name='dummy-repo'))
