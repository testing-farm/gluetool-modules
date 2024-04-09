# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import attrs
from xml.sax.saxutils import escape

from typing import List, Optional, TYPE_CHECKING

from xsdata_attrs.bindings import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig
from xsdata.formats.dataclass.serializers.writers import XmlEventWriter

if TYPE_CHECKING:
    from gluetool_modules_framework.libs.results import Results, TestSuite, TestCase


def _xml_friendly_encode(system_out: List[str]) -> List[str]:
    encoded_out = []

    for line in system_out:
        # Replace special XML characters
        encoded_str = escape(line, {"'": "&apos;", '"': "&quot;"})

        # Replace non-printable characters (except tab, newline, carriage return)
        encoded_out.append(''.join(
            ch if 0x20 <= ord(ch) <= 0x7E or ch in ('\t', '\n', '\r') else f'&#{ord(ch)};'
            for ch in encoded_str
        ))

    return encoded_out


@attrs.define(kw_only=True)
class XUnitFailure:
    type: str = attrs.field(metadata={'type': 'Attribute'})
    # Do not show message in representation, it can be huge
    message: str = attrs.field(metadata={'type': 'Attribute'}, repr=False)

    @classmethod
    def construct(cls, test_case: 'TestCase') -> 'XUnitFailure':
        return XUnitFailure(type='FAIL', message='Test "{}" failed.'.format(test_case.name))


@attrs.define(kw_only=True)
class XUnitTestCase:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    # Do not show system_out in representation, it can be huge
    system_out: List[str] = attrs.field(metadata={'name': 'system-out'}, repr=False)
    # Ignore PEP8Bear  # Coala doesn't like this line for some reason
    classname: str = attrs.field(default='tests', metadata={'type': 'Attribute'})
    failure: Optional[XUnitFailure] = attrs.field(default=None)
    time: Optional[int] = attrs.field(default=None, metadata={'type': 'Attribute'})
    start_time: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute', 'name': 'start-time'})
    end_time: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute', 'name': 'end-time'})

    @classmethod
    def construct(cls, test_case: 'TestCase') -> 'XUnitTestCase':
        return XUnitTestCase(
            name=test_case.name,
            system_out=_xml_friendly_encode(test_case.system_out),
            failure=XUnitFailure.construct(test_case) if test_case.failure or test_case.error else None,
            time=int(test_case.duration.total_seconds()) if test_case.duration is not None else None,
            start_time=test_case.start_time,
            end_time=test_case.end_time,
        )


@attrs.define(kw_only=True)
class XUnitTestSuite:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    tests: str = attrs.field(metadata={'type': 'Attribute'})
    failures: str = attrs.field(metadata={'type': 'Attribute'})
    errors: str = attrs.field(metadata={'type': 'Attribute'})
    skipped: str = attrs.field(metadata={'type': 'Attribute'})
    testcase: List[XUnitTestCase]

    @classmethod
    def construct(cls, test_suite: 'TestSuite') -> 'XUnitTestSuite':
        return XUnitTestSuite(
            name=test_suite.name,
            tests=str(test_suite.test_count),
            failures=str(test_suite.failure_count),
            errors=str(test_suite.error_count),
            skipped=str(test_suite.skipped_count),
            testcase=[XUnitTestCase.construct(test_case) for test_case in test_suite.test_cases]
        )


@attrs.define(kw_only=True)
class XUnitTestSuites:
    """
    Root element of xunit tree - data model representing the resulting xunit XML structure with ability to serialize
    itself from the knowledge of the know-it-all `Results` tree.
    """

    class Meta:
        name = 'testsuites'
    testsuite: List[XUnitTestSuite]

    @classmethod
    def construct(cls, results: 'Results') -> 'XUnitTestSuites':
        return XUnitTestSuites(
            testsuite=[XUnitTestSuite.construct(test_suite) for test_suite in results.test_suites]
        )

    def to_xml_string(self, pretty_print: bool = False) -> str:
        return XmlSerializer(
            config=SerializerConfig(pretty_print=pretty_print, pretty_print_indent=' '),
            writer=XmlEventWriter
        ).render(self)
