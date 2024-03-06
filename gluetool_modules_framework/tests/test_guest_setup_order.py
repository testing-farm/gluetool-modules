# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from mock import MagicMock

import gluetool
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage
import gluetool_modules_framework.helpers.guest_setup_order
from gluetool_modules_framework.testing_farm.testing_farm_request import Artifact

from . import create_module, patch_shared


def mock_guest(execute_mock, artifacts=None, environment=None):
    guest_mock = MagicMock()
    guest_mock.name = 'guest0'
    guest_mock.execute = execute_mock
    guest_mock.environment = environment or TestingEnvironment()
    guest_mock.environment.arch = 'x86_64'
    guest_mock.environment.artifacts = [
        Artifact(
            id='123123123',
            packages=None,
            order=10,
            type='fedora-koji-build'
        ),
        Artifact(
            id='345345345',
            packages=None,
            order=10,
            type='fedora-koji-build'
        ),
        Artifact(
            id='foo',
            packages=None,
            order=50,
            type='repository'
        ),
        Artifact(
            id='foo',
            packages=None,
            order=30,
            type='fedora-copr-build'
        ),
    ]
    if artifacts:
        guest_mock.environment.artifacts += artifacts

    return guest_mock


@pytest.fixture(name='module')
def fixture_module(monkeypatch):
    module = create_module(gluetool_modules_framework.helpers.guest_setup_order.GuestSetupOrder)[1]

    module._config['artifact-guest-setup-map'] = 'foo-map'

    pattern_map_mock = MagicMock(match=MagicMock)

    def mock_match(pattern, **kwargs):
        if pattern == 'wrongtype':
            raise gluetool.glue.GlueError('Pattern not found')
        return 'guest-setup-{}'.format(pattern)

    pattern_map_mock.match = mock_match

    monkeypatch.setattr(gluetool_modules_framework.helpers.guest_setup_order,
                        'PatternMap', MagicMock(return_value=pattern_map_mock))

    patch_shared(monkeypatch, module, {}, callables={
        'setup_guest': None,
        'setup-guest-fedora-koji-build': MagicMock(return_values='setup-guest-fedora-koji-build'),
        'setup-guest-fedora-copr-build': MagicMock(return_values='setup-guest-fedora-copr-build'),
        'setup-guest-redhat-brew-build': MagicMock(return_values='setup-guest-redhat-brew-build'),
        'setup-guest-repository': MagicMock(return_values='setup-guest-repository'),
        'setup-guest-repository-file': MagicMock(return_values='setup-guest-repository-file'),
    })

    return module


def test_sanity_shared(module):
    assert module.glue.has_shared('setup_guest') is True


def test_guest_setup(monkeypatch, module, log):

    koji_mock = MagicMock(return_values=MagicMock(stdout='setup-guest-fedora-koji-build', stderr=''))
    copr_mock = MagicMock(return_values=MagicMock(stdout='setup-guest-fedora-copr-build', stderr=''))
    repository_mock = MagicMock(return_values=MagicMock(stdout='setup-guest-repository', stderr=''))

    patch_shared(monkeypatch, module, {}, callables={
        'setup_guest': None,
        'guest-setup-fedora-koji-build': koji_mock,
        'guest-setup-fedora-copr-build': copr_mock,
        'guest-setup-repository': repository_mock,
    })

    execute_mock = MagicMock(return_value=MagicMock(stdout='', stderr=''))
    guest = mock_guest(execute_mock)

    module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION,
                       log_dirpath=str('tmpdir'))

    assert log.records[0].message == "attempt to map artifact type 'fedora-koji-build' to guest setup function using 'foo-map' pattern map"
    assert log.records[1].message == "artifact type 'fedora-koji-build' was mapped to 'guest-setup-fedora-koji-build'"
    assert log.records[2].message == "Running guest setup for fedora-koji-build artifact"
    assert log.records[3].message == "attempt to map artifact type 'fedora-copr-build' to guest setup function using 'foo-map' pattern map"
    assert log.records[4].message == "artifact type 'fedora-copr-build' was mapped to 'guest-setup-fedora-copr-build'"
    assert log.records[5].message == "Running guest setup for fedora-copr-build artifact"
    assert log.records[6].message == "attempt to map artifact type 'repository' to guest setup function using 'foo-map' pattern map"
    assert log.records[7].message == "artifact type 'repository' was mapped to 'guest-setup-repository'"
    assert log.records[8].message == "Running guest setup for repository artifact"

    guest = mock_guest(execute_mock, artifacts=[Artifact(id='wrongid', order=60, type='wrongtype')])

    with pytest.raises(gluetool.glue.GlueError, match='Pattern not found'):
        module.setup_guest(guest, stage=GuestSetupStage.ARTIFACT_INSTALLATION,
                           log_dirpath=str('tmpdir'))
