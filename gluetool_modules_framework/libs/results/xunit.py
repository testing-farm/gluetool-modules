# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import attrs

from typing import List, Optional, TYPE_CHECKING

from xsdata_attrs.bindings import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

from gluetool_modules_framework.libs.results.xml import XmlWriter

if TYPE_CHECKING:
    from gluetool_modules_framework.libs.results import Results, TestSuite, TestCase


@attrs.define
class XUnitFailure:
    type: str = attrs.field(metadata={'type': 'Attribute'})
    message: str = attrs.field(metadata={'type': 'Attribute'})

    @classmethod
    def construct(cls, test_case: 'TestCase') -> 'XUnitFailure':
        return XUnitFailure(type='FAIL', message='Test "{}" failed.'.format(test_case.name))


@attrs.define
class XUnitTestCase:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    system_out: List[str] = attrs.field(metadata={'name': 'system-out'})
    # Ignore PEP8Bear  # Coala doesn't like this line for some reason
    classname: str = attrs.field(default='tests', metadata={'type': 'Attribute'})
    failure: Optional[XUnitFailure] = attrs.field(default=None)

    @classmethod
    def construct(cls, test_case: 'TestCase') -> 'XUnitTestCase':
        return XUnitTestCase(
            name=test_case.name,
            system_out=test_case.system_out,
            failure=XUnitFailure.construct(test_case) if test_case.failure or test_case.error else None
        )


@attrs.define
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


@attrs.define
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
        return XmlSerializer(config=SerializerConfig(pretty_print=pretty_print), writer=XmlWriter).render(self)
