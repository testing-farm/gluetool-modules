# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from gluetool_modules_framework.libs.results.xunit_testing_farm import XUnitTFTestSuites
from gluetool_modules_framework.libs.results.xunit import XUnitTestSuites
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from gluetool_modules_framework.infrastructure.koji_fedora import KojiTask
from gluetool_modules_framework.infrastructure.copr import CoprTask

import attrs
import datetime

from typing import Any, Dict, List, Optional, Union


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
    end_time: Optional[str]
    logs: List[Log]


@attrs.define(frozen=True)
class Property:
    name: str
    value: str


@attrs.define(frozen=True)
class FmfId:
    url: Optional[str]
    ref: Optional[str]
    name: Optional[str]
    path: Optional[str]


@attrs.define
class AIAnalysisSnippet:
    """
    A single log snippet Log Detective flagged as relevant to a failure.

    Mirrors one entry of the ``snippets`` array in the analysis result returned by the Log
    Detective ``/analyze`` API. Instances are built by the ``log-detective`` module
    (:py:mod:`gluetool_modules_framework.helpers.logdetective`) from that JSON response via
    :py:meth:`AIAnalysis.from_api_response`.
    """

    #: The snippet text extracted from the log (``snippets[].text``).
    text: str
    #: Line number the snippet starts at, if reported (``snippets[].line_number``).
    line_number: Optional[int] = None
    #: Name of the source log the snippet came from, if reported (``snippets[].source_file``).
    source_file: Optional[str] = None


@attrs.define
class AIAnalysis:
    """
    Structured AI failure analysis for a test case or plan.

    This mirrors the subset of fields we surface from the JSON analysis result returned by the
    Log Detective ``/analyze`` API. Instances are produced by the ``log-detective`` module
    (:py:mod:`gluetool_modules_framework.helpers.logdetective`) from that response — see
    :py:meth:`from_api_response` — and attached to :py:attr:`TestCase.ai_summary` (per failing
    test) or :py:attr:`TestSuite.ai_summary` (plan-level fallback).
    """

    #: Human-readable explanation of the failure (Log Detective ``explanation``).
    explanation: str
    #: Suggested remediation, if the API provided one (Log Detective ``solution``).
    solution: Optional[str] = None
    #: Whether Log Detective concluded there was no actual issue (Log Detective ``no_issue_found``).
    no_issue_found: bool = False
    #: Log snippets the model considered relevant (Log Detective ``snippets``).
    snippets: List[AIAnalysisSnippet] = attrs.field(factory=list)

    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> 'AIAnalysis':
        """
        Build an :py:class:`AIAnalysis` from a completed task's analysis result.

        The shape of ``data`` is the Log Detective analysis response: plain-text ``explanation``
        and optional plain-text ``solution``, a ``no_issue_found`` boolean,
        and a ``snippets`` list of ``{text, line_number, source_file}`` objects. Missing fields
        degrade gracefully so a partial response still yields a usable structure. Text fields
        are stripped of XML 1.0-forbidden characters before the report is serialized.

        :param data: The decoded analysis result from a completed Log Detective task.
        :returns: The corresponding :py:class:`AIAnalysis`.
        """
        # XML 1.0 permits TAB, LF, CR and printable scalar values. Model output may
        # contain control characters or surrogates, which make the entire report invalid.
        def xml_text(value: Any) -> str:
            if not isinstance(value, str):
                return ''
            return ''.join(char for char in value if char in '\t\n\r' or
                           '\x20' <= char <= '\ud7ff' or '\ue000' <= char <= '\ufffd' or
                           '\U00010000' <= char <= '\U0010ffff')

        snippets = [
            AIAnalysisSnippet(
                text=xml_text(snippet.get('text')),
                line_number=snippet.get('line_number'),
                source_file=xml_text(snippet['source_file']) if snippet.get('source_file') is not None else None,
            )
            for snippet in (data.get('snippets') or [])
        ]

        return cls(
            explanation=xml_text(data.get('explanation')),
            solution=xml_text(data['solution']) if data.get('solution') is not None else None,
            no_issue_found=bool(data.get('no_issue_found', False)),
            snippets=snippets,
        )


@attrs.define
class TestCase:
    name: str
    result: Optional[str] = None
    subresults: List[TestCaseSubresult] = attrs.field(factory=list)
    note: List[str] = attrs.field(factory=list)
    # Structured AI analysis (from the Log Detective /analyze API) of this test case's failure logs.
    ai_summary: Optional[AIAnalysis] = None
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
    # Plan-level error reason, used when the whole plan failed (e.g. during guest-setup) before any test cases
    # were produced. True can be used to display a blank element, string for a message.
    error: Union[bool, str] = False
    # Link to the log of the failing phase, surfaced on the plan-level error element.
    error_log_name: Optional[str] = None
    error_log_href: Optional[str] = None
    # Structured AI analysis (from the Log Detective /analyze API) for plan-level infrastructure failures.
    ai_summary: Optional[AIAnalysis] = None

    @property
    def test_count(self) -> int:
        return len(self.test_cases)

    @property
    def failure_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('failed', 'fail', 'fail:',
                                                                                       'needs_inspection')])

    @property
    def error_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('error', 'errored', 'error:')])

    @property
    def skipped_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('skip', 'skipped', 'skip:')])

    @property
    def pending_count(self) -> int:
        return len([test_case for test_case in self.test_cases if test_case.result in ('pending', 'pending:')])


@attrs.define
class Results:
    """
    Root element of know-it-all tree - data model containing all available information that might be needed to serialize
    into various resulting structures.
    """

    overall_result: Optional[str] = None
    test_suites: List[TestSuite] = attrs.field(factory=list)

    # Short, high-level summary of the run (e.g. "3 plans failed, 2 plans errored out"). Reported as the top-level
    # request summary when the pipeline completes without raising. Not serialized into xUnit.
    summary: Optional[str] = None

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
