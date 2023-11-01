# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from gluetool_modules_framework.libs.results.xunit_testing_farm import XUnitTFTestSuites
from gluetool_modules_framework.libs.results.xunit import XUnitTestSuites
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from gluetool_modules_framework.infrastructure.koji_fedora import KojiTask
from gluetool_modules_framework.infrastructure.copr import CoprTask

import attrs

from typing import List, Dict, Optional, Union


@attrs.define
class Log:
    href: str
    name: str
    guest_setup_stage: Optional[str] = None
    schedule_stage: Optional[str] = None
    schedule_entry: Optional[str] = None


# Used in BaseOS CI results
@attrs.define
class Phase:
    name: str
    result: str
    time: Optional[str]
    logs: List[Log] = attrs.field(factory=list)


# Used in multihost pipeline results
@attrs.define
class Guest:
    name: str
    role: Optional[str] = None
    environment: Optional[TestingEnvironment] = None


@attrs.define
class TestCase:
    name: str
    result: Optional[str] = None
    note: Optional[str] = None
    properties: Dict[str, str] = attrs.field(factory=dict)
    logs: List[Log] = attrs.field(factory=list)
    requested_environment: Optional[TestingEnvironment] = None
    provisioned_environment: Optional[TestingEnvironment] = None
    # True can be used just to display a blank failure element, string can be specified as a failure message
    failure: Union[bool, str] = False
    error: Union[bool, str] = False
    system_out: List[str] = attrs.field(factory=list)

    # Properties used in BaseOS CI results.xml
    # TODO: float would be a more suitable type, fix this when we add time property also to Testing Farm results, str
    # should be only on the next level - classes representing the XML layer
    time: Optional[str] = None
    parameters: List[str] = attrs.field(factory=list)
    phases: List[Phase] = attrs.field(factory=list)
    packages: Optional[List[str]] = None
    test_outputs: Optional[List[str]] = None

    # Properties used in BaseOS CI covscan module
    added: Optional[str] = None
    fixed: Optional[str] = None
    baseline: Optional[str] = None
    result_class: Optional[str] = None
    test_type: Optional[str] = None
    defects: Optional[str] = None

    # Used in multihost pipeline
    serial_number: Optional[int] = None
    guest: Optional[Guest] = None


@attrs.define
class TestSuite:
    name: str
    result: Optional[str] = None
    logs: List[Log] = attrs.field(factory=list)
    properties: Dict[str, str] = attrs.field(factory=dict)
    test_cases: List[TestCase] = attrs.field(factory=list)
    requested_environment: Optional[TestingEnvironment] = None
    provisioned_environment: Optional[TestingEnvironment] = None
    guests: List[Guest] = attrs.field(factory=list)

    @property
    def test_count(self) -> int:
        return len(self.test_cases)

    @property
    def failure_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('failed', 'fail', 'fail:',
                                                                                       'needs_inspection', 'error',
                                                                                       'errored', 'error:')])

    @property
    def error_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('error', 'errored', 'error:')])

    @property
    def skipped_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('error', 'errored', 'error:')])


@attrs.define
class Results:
    """
    Root element of know-it-all tree - data model containing all available information that might be needed to serialize
    into various resulting structures.
    """

    overall_result: Optional[str] = None
    test_suites: List[TestSuite] = attrs.field(factory=list)

    primary_task: Optional[Union[KojiTask, CoprTask]] = None

    test_schedule_result: Optional[str] = None

    testing_thread: Optional[str] = None

    polarion_lookup_method: Optional[str] = None
    polarion_custom_lookup_method_field_id: Optional[str] = None
    polarion_project_id: Optional[str] = None

    @property
    def xunit_testing_farm(self) -> XUnitTFTestSuites:
        return XUnitTFTestSuites.construct(self)

    @property
    def xunit(self) -> XUnitTestSuites:
        return XUnitTestSuites.construct(self)
