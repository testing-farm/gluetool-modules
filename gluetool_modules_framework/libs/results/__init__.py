# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from gluetool_modules_framework.libs.results.xunit_testing_farm import XUnitTFTestSuites
from gluetool_modules_framework.libs.results.xunit import XUnitTestSuites
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from gluetool_modules_framework.infrastructure.koji_fedora import KojiTask
from gluetool_modules_framework.infrastructure.copr import CoprTask

import attrs
import datetime

from typing import List, Optional, Union


@attrs.define
class Log:
    href: str = attrs.field(eq=True)
    name: str = attrs.field(eq=True)
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
class TestCaseCheck:
    name: str
    result: str
    event: str
    logs: List[Log]


@attrs.define
class TestCaseSubresult:
    name: str
    result: str
    original_result: str
    end_time: str
    logs: List[Log]


@attrs.define(frozen=True)
class Property:
    name: str
    value: str


@attrs.define(frozen=True)
class FmfId:
    url: str
    ref: str
    name: str
    path: Optional[str]


@attrs.define
class TestCase:
    name: str
    result: Optional[str] = None
    subresults: List[TestCaseSubresult] = attrs.field(factory=list)
    note: List[str] = attrs.field(factory=list)
    properties: List[Property] = attrs.field(factory=list)
    logs: List[Log] = attrs.field(factory=list)
    requested_environment: Optional[TestingEnvironment] = None
    provisioned_environment: Optional[TestingEnvironment] = None
    # True can be used just to display a blank failure element, string can be specified as a failure message
    failure: Union[bool, str] = False
    error: Union[bool, str] = False
    # Do not show system_out in representation, it can be huge
    system_out: List[str] = attrs.field(factory=list, repr=False)
    checks: List[TestCaseCheck] = attrs.field(factory=list)
    duration: Optional[datetime.timedelta] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    fmf_id: Optional[FmfId] = None

    # Properties used in BaseOS CI results.xml
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

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def check_error_count(self) -> int:
        return len([check for check in self.checks if check.result == 'error'])

    @property
    def check_failure_count(self) -> int:
        return len([check for check in self.checks if check.result == 'fail'])


@attrs.define
class TestSuite:
    name: str
    result: Optional[str] = None
    logs: List[Log] = attrs.field(factory=list)
    properties: List[Property] = attrs.field(factory=list)
    test_cases: List[TestCase] = attrs.field(factory=list)
    requested_environment: Optional[TestingEnvironment] = None
    provisioned_environment: Optional[TestingEnvironment] = None
    guests: List[Guest] = attrs.field(factory=list)
    stage: Optional[str] = None

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
