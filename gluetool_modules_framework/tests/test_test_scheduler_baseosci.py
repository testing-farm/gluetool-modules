# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import pytest

from mock import MagicMock
from gluetool import GlueError
from gluetool.log import Logging

import gluetool_modules_framework.testing.test_scheduler_baseosci
from gluetool_modules_framework.testing.test_scheduler_baseosci import NoTestableArtifactsError

from gluetool_modules_framework.helpers.rules_engine import RulesEngine
from gluetool_modules_framework.libs import _UniqObject, ANY
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleEntry
from gluetool_modules_framework.libs.guest import NetworkedGuest

from . import create_module, patch_shared, testing_asset

from dataclasses import dataclass
from typing import List, Optional, Tuple, Union


@dataclass
class EnvironmentRequestedMock:
    arch: str


@dataclass
class ProvisionerCapabilitiesMock:
    available_arches: List[str]


@dataclass
class ArchesMock:
    arches: List[str]


@dataclass
class PrimaryTaskMock:
    task_arches: ArchesMock
    ARTIFACT_NAMESPACE: str


@dataclass
class TestCase:
    # ID of the test displayed by pytest
    id: str
    # Requested environments
    environments_requested: List[EnvironmentRequestedMock]
    # Available composes
    composes: List[str]
    # Primary task arches
    primary_task_arches: Optional[List[str]] = None
    # Available arches for provisioner
    provisioner_available_arches: Union[List[str], _UniqObject] = ANY
    # Arch compatiblity map filename, relative to `assets/test_schedule_baseosci`
    arch_compatibility_map: Optional[str] = None
    # TEC patch map filename, relative to `assets/test_schedule_baseosci`
    tec_patch_map: Optional[str] = None
    # Compose constraint arches map filename, relative to `assets/test_schedule_baseosci`
    compose_constraint_arches_map: Optional[str] = None
    # Without arches list
    without_arches: Optional[List[str]] = None  # Ignore PEP8Bear
    # Expected schedule
    expected_schedule: Optional[List[TestingEnvironment]] = None
    # Expected log messages
    expected_messages: Optional[Tuple[int, str]] = None
    # Expected exception during execute
    expected_exception: Optional[Tuple[Exception, str]] = None


def create_test_schedule_mock(testing_environment_constraints=None):
    test_schedule = TestSchedule()
    for tec in testing_environment_constraints:
        entry = TestScheduleEntry(Logging.get_logger(), 'some-entry-id', 'some-capability')
        entry.testing_environment = tec
        entry.guest = NetworkedGuest(MagicMock(), 'name')
        entry.guest.environment = tec
        entry.arch = tec.arch
        test_schedule.append(entry)
    return test_schedule


def evaluate_instructions_mock(
    instructions, commands,
    context=None, default_rule='True', stop_at_first_hit=False, ignore_unhandled_commands=False
):
    return None


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.testing.test_scheduler_baseosci.TestSchedulerBaseOSCI)[1]


@pytest.fixture(name='rules_engine')
def fixture_rules_engine():
    return create_module(gluetool_modules_framework.helpers.rules_engine.RulesEngine)[1]


@pytest.mark.parametrize('tc', [
    #
    # All constraints set, arch and noarch
    #
    TestCase(
        id='all_arch_noarch',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
            EnvironmentRequestedMock(arch='s390x'),
        ],
        primary_task_arches=['noarch', 'x86_64', 's390x'],
        composes=['RHEL', 'Fedora'],
        provisioner_available_arches=['x86_64', 'aarch64', 's390x', 'ppc64le'],
        arch_compatibility_map='arch-compatibility-map.yaml',
        tec_patch_map='tec-patch-map.yaml',
        compose_constraint_arches_map='compose-constraints-arches-map.yaml',
        expected_schedule=[
            TestingEnvironment('aarch64', compose='RHEL', snapshots=True),
            TestingEnvironment('aarch64', compose='Fedora', snapshots=True),
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ],
        expected_messages=[
            (logging.WARN, "Artifact arch 'noarch' not supported but compatible with 'x86_64, s390x, ppc64le, aarch64'"),
            (logging.WARN, "Artifact has 'noarch' bits side by side with regular bits (noarch, x86_64, s390x)")
        ]
    ),

    #
    # All constraints set, noarch only
    #
    TestCase(
        id='all_noarch_only',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
            EnvironmentRequestedMock(arch='s390x')
        ],
        primary_task_arches=['noarch'],
        composes=['RHEL', 'Fedora'],
        provisioner_available_arches=['x86_64', 'aarch64', 's390x', 'ppc64le'],
        arch_compatibility_map='arch-compatibility-map.yaml',
        tec_patch_map='tec-patch-map.yaml',
        compose_constraint_arches_map='compose-constraint-arches-map.yaml',
        expected_schedule=[
            TestingEnvironment('aarch64', compose='RHEL', snapshots=True),
            TestingEnvironment('aarch64', compose='Fedora', snapshots=True),
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ],
        expected_messages=[
            (logging.WARN, "Artifact arch 'noarch' not supported but compatible with 'x86_64, s390x, ppc64le, aarch64'"),
        ]
    ),

    #
    # All constraints set, noarch only, ANY supported architectures
    #
    TestCase(
        id='all_noarch_only_any',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
            EnvironmentRequestedMock(arch='s390x'),
        ],
        primary_task_arches=['noarch'],
        composes=['RHEL', 'Fedora'],
        arch_compatibility_map='arch-compatibility-map.yaml',
        tec_patch_map='tec-patch-map.yaml',
        compose_constraint_arches_map='compose-constraint-arches-map.yaml',
        expected_messages=[
            (logging.WARN, "Artifact arch 'noarch' not supported but compatible with 'x86_64, s390x, ppc64le, aarch64'"),
        ],
        expected_exception=(GlueError, 'Test schedule is empty')
    ),

    #
    # All constraints set, arch, no noarch
    #
    TestCase(
        id='all_arch_no_noarch',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
            EnvironmentRequestedMock(arch='s390x'),
        ],
        primary_task_arches=['x86_64', 's390x'],
        composes=['RHEL', 'Fedora'],
        provisioner_available_arches=['x86_64', 'aarch64', 's390x', 'ppc64le'],
        arch_compatibility_map='arch-compatibility-map.yaml',
        tec_patch_map='tec-patch-map.yaml',
        compose_constraint_arches_map='compose-constraint-arches-map.yaml',
        expected_schedule=[
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ]
    ),

    #
    # No mapping files
    #
    TestCase(
        id='no_mapping_files',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
            EnvironmentRequestedMock(arch='s390x'),
        ],
        primary_task_arches=['x86_64', 's390x', 'ppc64le'],
        composes=['RHEL', 'Fedora'],
        provisioner_available_arches=['x86_64', 'aarch64', 's390x', 'ppc64le'],
        expected_schedule=[
            TestingEnvironment('ppc64le', compose='RHEL', snapshots=True),
            TestingEnvironment('ppc64le', compose='Fedora', snapshots=True),
            TestingEnvironment('s390x', compose='RHEL', snapshots=True),
            TestingEnvironment('s390x', compose='Fedora', snapshots=True),
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ]
    ),

    #
    # No constraints
    #
    TestCase(
        id='no_constraints',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
        ],
        composes=['RHEL', 'Fedora'],
        expected_exception=(GlueError, 'No valid arches found for given constraints, cannot continue')
    ),

    #
    # No testable artifacts
    #
    TestCase(
        id='no_testable_artifacts',
        environments_requested=[
            EnvironmentRequestedMock(arch='aarch64'),
        ],
        primary_task_arches=['aarch64'],
        composes=['RHEL', 'Fedora'],
        provisioner_available_arches=['x86_64'],
        expected_exception=(
            NoTestableArtifactsError,
            'Task does not have any testable artifact - aarch64 arches are not supported'
        )
    ),

    #
    # No primary_task
    #
    TestCase(
        id='no_primary_task',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
            EnvironmentRequestedMock(arch='s390x'),
        ],
        composes=['RHEL', 'Fedora'],
        provisioner_available_arches=['x86_64', 'aarch64', 's390x', 'ppc64le'],
        arch_compatibility_map='arch-compatibility-map.yaml',
        tec_patch_map='tec-patch-map.yaml',
        compose_constraint_arches_map='compose-constraint-arches-map.yaml',
        expected_schedule=[
            TestingEnvironment('aarch64', compose='RHEL', snapshots=True),
            TestingEnvironment('aarch64', compose='Fedora', snapshots=True),
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ],
        expected_messages=[
            (logging.WARN, 'No artifact arches found, using all supported ones')
        ],
    ),

    #
    # Without arches
    #
    TestCase(
        id='without_arches',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
        ],
        primary_task_arches=['x86_64', 'aarch64'],
        composes=['RHEL', 'Fedora'],
        tec_patch_map='tec-patch-map.yaml',
        without_arches=['aarch64'],
        expected_schedule=[
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ],
        expected_messages=[
            (logging.DEBUG, 'testing constraint arch=aarch64,compose=Fedora,snapshots=True dropped by --without-arch'),
        ]
    ),

    #
    # Test arches patching
    #
    TestCase(
        id='patched_arches',
        environments_requested=[
            EnvironmentRequestedMock(arch='x86_64'),
            EnvironmentRequestedMock(arch='aarch64'),
        ],
        primary_task_arches=['patched-arch'],
        composes=['RHEL', 'Fedora'],
        tec_patch_map='tec-patch-map-patch-arch.yaml',
        expected_schedule=[
            TestingEnvironment('x86_64', compose='RHEL', snapshots=True),
            TestingEnvironment('x86_64', compose='Fedora', snapshots=True)
        ],
        expected_messages=[
        ]
    ),

], ids=lambda testcase: testcase.id)
def test_execute_extended(module, rules_engine, monkeypatch, log, tc):

    callables = {
        'evaluate_instructions': rules_engine.evaluate_instructions,
        'create_test_schedule': create_test_schedule_mock,
        'compose': lambda: tc.composes,
        'actual_compose': lambda _: ''
    }

    if tc.provisioner_available_arches:
        callables.update({
            'provisioner_capabilities': lambda: ProvisionerCapabilitiesMock(available_arches=tc.provisioner_available_arches),
        })

    if tc.primary_task_arches:
        primary_task_mock = PrimaryTaskMock(ArchesMock(tc.primary_task_arches), 'some-namespace')
        callables.update({
            'primary_task': lambda: primary_task_mock,
            'tasks': lambda: [primary_task_mock]
        })

    patch_shared(monkeypatch, module, {}, callables=callables)

    has_artifacts_mock = MagicMock()
    monkeypatch.setattr(gluetool_modules_framework.libs.artifacts, 'has_artifacts', has_artifacts_mock)

    module._config.update({
        'with-arch': [],
        'without-arch': [],
        'use-snapshots': True,
    })

    if tc.arch_compatibility_map:
        module._config.update({
            'arch-compatibility-map': testing_asset('test_schedule_baseosci', tc.arch_compatibility_map),
        })

    if tc.tec_patch_map:
        module._config.update({
            'tec-patch-map': testing_asset('test_schedule_baseosci', tc.tec_patch_map),
        })

    if tc.compose_constraint_arches_map:
        module._config.update({
            'compose-constraint-arches-map': testing_asset('test_schedule_baseosci', tc.compose_constraint_arches_map)
        })

    if tc.expected_exception:
        exception, message = tc.expected_exception
        with pytest.raises(exception, match=message):
            module.execute()
        return

    if tc.without_arches:
        module._config.update({
            'without-arch': ','.join(tc.without_arches)
        })

    module.execute()

    # sort schedules to get and expected order for testing
    module._schedule.sort(key=lambda s: s.arch)
    assert [entry.testing_environment for entry in module.test_schedule()] == tc.expected_schedule

    if not tc.expected_messages:
        return

    for level, message in tc.expected_messages:
        assert log.match(levelno=level, message=message)


def test_no_testable_artifact():
    primary_task = PrimaryTaskMock(ArchesMock(['some-arch']), 'some-namespace')
    exception = NoTestableArtifactsError(primary_task, ['some-other-arch'])

    assert exception.task_arches == ['some-arch']
    assert exception.supported_arches == ['some-other-arch']
    assert exception.submit_to_sentry == False
