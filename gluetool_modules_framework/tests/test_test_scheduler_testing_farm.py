# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
from gluetool import GlueError
import gluetool_modules_framework.testing.test_scheduler_testing_farm

from . import create_module, patch_shared

from dataclasses import dataclass
from typing import List


@dataclass
class EnvironmentRequestedMock:
    arch: str


@dataclass
class TestingFarmRequestMock:
    __test__ = False
    environments_requested: List[EnvironmentRequestedMock]


@dataclass
class ProvisionerCapabilitiesMock:
    available_arches: List[str]


def create_test_schedule_mock(testing_environment_constraints=None):
    return testing_environment_constraints or []


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.testing.test_scheduler_testing_farm.TestSchedulerTestingFarm)[1]


@pytest.mark.parametrize('environments_requested, provisioner_available_arches, expected_schedule', [
    ([EnvironmentRequestedMock(arch='foo')], ['foo', 'bar'], [EnvironmentRequestedMock(arch='foo')]),
    ([EnvironmentRequestedMock(arch='foo')], gluetool_modules_framework.libs.ANY, [EnvironmentRequestedMock(arch='foo')]),
])
def test_execute(module, monkeypatch, environments_requested, provisioner_available_arches, expected_schedule):
    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: TestingFarmRequestMock(environments_requested=environments_requested),
        'create_test_schedule': create_test_schedule_mock,
        'provisioner_capabilities': lambda: ProvisionerCapabilitiesMock(available_arches=provisioner_available_arches)
    })
    module.execute()
    assert module.test_schedule() == expected_schedule


@pytest.mark.parametrize('environments_requested, provisioner_available_arches, expected_error', [
    (
        [EnvironmentRequestedMock(arch='foo')],
        [],
        "The architecture 'foo' is unsupported, cannot continue"
    ),
    (
        [EnvironmentRequestedMock(arch='foo'), EnvironmentRequestedMock(arch='hello')],
        ['bar', 'baz'],
        "The architecture 'foo' is unsupported, cannot continue"
    ),
    (
        [],
        ['bar', 'baz'],
        'Test schedule is empty'
    ),
])
def test_execute_error(module, monkeypatch, environments_requested, provisioner_available_arches, expected_error):
    patch_shared(monkeypatch, module, {}, callables={
        'testing_farm_request': lambda: TestingFarmRequestMock(environments_requested=environments_requested),
        'create_test_schedule': create_test_schedule_mock,
        'provisioner_capabilities': lambda: ProvisionerCapabilitiesMock(available_arches=provisioner_available_arches)
    })
    try:
        module.execute()
    except GlueError as exc:
        assert str(exc) == expected_error
