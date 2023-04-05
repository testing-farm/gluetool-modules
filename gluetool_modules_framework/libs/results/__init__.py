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


@attrs.define
class TestCase:
    name: str
    result: str
    properties: Dict[str, str] = attrs.field(factory=dict)
    logs: List[Log] = attrs.field(factory=list)
    requested_environment: Optional[TestingEnvironment] = None
    provisioned_environment: Optional[TestingEnvironment] = None
    failure: bool = False
    error: bool = False
    system_out: List[str] = attrs.field(factory=list)


@attrs.define
class TestSuite:
    name: str
    result: str
    logs: List[Log] = attrs.field(factory=list)
    properties: Dict[str, str] = attrs.field(factory=dict)
    test_cases: List[TestCase] = attrs.field(factory=list)

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
