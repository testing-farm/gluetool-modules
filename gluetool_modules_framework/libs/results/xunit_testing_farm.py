# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import attrs

from xsdata_attrs.bindings import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

from typing import List, Optional, Dict, TYPE_CHECKING, cast

from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from gluetool_modules_framework.libs.results.xml import XmlWriter

if TYPE_CHECKING:
    from gluetool_modules_framework.libs.results import Results, TestSuite, TestCase, Log  # noqa


@attrs.define
class XUnitTFProperty:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    value: str = attrs.field(metadata={'type': 'Attribute'})


@attrs.define
class XUnitTFProperties:
    property: List[XUnitTFProperty]

    @classmethod
    def construct(cls, properties: Dict[str, str]) -> 'XUnitTFProperties':
        return XUnitTFProperties(
            property=[XUnitTFProperty(name=name, value=value) for name, value in properties.items()]
        )


@attrs.define
class XUnitTFLog:
    href: str = attrs.field(metadata={'type': 'Attribute'})
    name: str = attrs.field(metadata={'type': 'Attribute'})
    schedule_stage: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute', 'name': 'schedule-stage'})
    schedule_entry: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute', 'name': 'schedule-entry'})
    guest_setup_stage: Optional[str] = attrs.field(
        default=None,
        metadata={'type': 'Attribute', 'name': 'guest-setup-stage'}
    )


@attrs.define
class XUnitTFLogs:
    log: List[XUnitTFLog]

    @classmethod
    def construct(cls, logs: List['Log']) -> 'XUnitTFLogs':
        return XUnitTFLogs(log=[XUnitTFLog(
            href=log.href,
            name=log.name,
            guest_setup_stage=log.guest_setup_stage,
            schedule_stage=log.schedule_stage,
            schedule_entry=log.schedule_entry
        ) for log in logs])


@attrs.define
class XUnitTFTestingEnvironment:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    property: List[XUnitTFProperty]

    @classmethod
    def construct(cls, testing_environment: 'TestingEnvironment', name: str) -> 'XUnitTFTestingEnvironment':
        properties: List[XUnitTFProperty] = []
        if testing_environment.arch is not None:
            properties.append(XUnitTFProperty(name='arch', value=cast(str, testing_environment.arch)))
        if testing_environment.compose is not None:
            properties.append(XUnitTFProperty(name='compose', value=cast(str, testing_environment.compose)))
        if testing_environment.snapshots is not None:
            properties.append(XUnitTFProperty(name='snapshots', value=str(testing_environment.snapshots)))

        return XUnitTFTestingEnvironment(name=name, property=properties)


@attrs.define
class XUnitTFTestCase:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    result: str = attrs.field(metadata={'type': 'Attribute'})
    properties: XUnitTFProperties
    logs: XUnitTFLogs
    testing_environment: List[XUnitTFTestingEnvironment] = attrs.field(
        factory=list,
        metadata={'name': 'testing-environment'}
    )
    failure: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def construct(cls, test_case: 'TestCase') -> 'XUnitTFTestCase':
        environments: List[XUnitTFTestingEnvironment] = []
        if test_case.requested_environment:
            environments.append(XUnitTFTestingEnvironment.construct(test_case.requested_environment, 'requested'))
        if test_case.provisioned_environment:
            environments.append(XUnitTFTestingEnvironment.construct(test_case.provisioned_environment, 'provisioned'))

        return XUnitTFTestCase(
            name=test_case.name,
            result=test_case.result,
            properties=XUnitTFProperties.construct(test_case.properties),
            logs=XUnitTFLogs.construct(test_case.logs),
            testing_environment=environments,
            # When the value is `None`, the XML element is not created at all, but when the value
            # is '', it gets created as an empty element.
            failure='' if test_case.failure else None,
            error='' if test_case.error else None,
        )


@attrs.define
class XUnitTFTestSuite:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    result: str = attrs.field(metadata={'type': 'Attribute'})
    tests: str = attrs.field(metadata={'type': 'Attribute'})
    logs: Optional[XUnitTFLogs] = None
    properties: Optional[XUnitTFProperties] = None
    testcase: List[XUnitTFTestCase] = attrs.field(factory=list)

    @classmethod
    def construct(cls, test_suite: 'TestSuite') -> 'XUnitTFTestSuite':
        return XUnitTFTestSuite(
            name=test_suite.name,
            result=test_suite.result,
            tests=str(test_suite.test_count),
            logs=XUnitTFLogs.construct(test_suite.logs) if test_suite.logs else None,
            properties=XUnitTFProperties.construct(test_suite.properties) if test_suite.properties else None,
            testcase=[XUnitTFTestCase.construct(test_case) for test_case in test_suite.test_cases],
        )

    def to_xml_string(self, pretty_print: bool = False) -> str:
        return XmlSerializer(config=SerializerConfig(pretty_print=pretty_print)).render(self)


@attrs.define
class XUnitTFTestSuites:
    """
    Root element of Testing Farm xunit tree - data model representing the resulting xunit XML structure with ability to
    serialize itself from the knowledge of the know-it-all `Results` tree.
    """

    class Meta:
        name = 'testsuites'

    overall_result: str = attrs.field(metadata={'type': 'Attribute', 'name': 'overall-result'})
    properties: XUnitTFProperties
    testsuite: List[XUnitTFTestSuite]

    @classmethod
    def construct(cls, results: 'Results') -> 'XUnitTFTestSuites':
        properties: Dict[str, str] = {}
        if results.primary_task:
            properties.update({
                'baseosci.artifact-id': str(results.primary_task),
                'baseosci.artifact-namespace': results.primary_task.ARTIFACT_NAMESPACE
            })

        assert results.test_schedule_result is not None
        properties.update({'baseosci.overall-result': results.test_schedule_result})

        if results.testing_thread:
            properties.update({'baseosci.id.testing-thread': results.testing_thread})

        if results.polarion_lookup_method is not None:
            assert results.polarion_custom_lookup_method_field_id is not None
            assert results.polarion_project_id is not None
            properties.update({
                'polarion-lookup-method': results.polarion_lookup_method,
                'polarion-custom-lookup-method-field-id': results.polarion_custom_lookup_method_field_id,
                'polarion-project-id': results.polarion_project_id
            })

        assert results.overall_result

        return XUnitTFTestSuites(
            overall_result=results.overall_result,
            properties=XUnitTFProperties.construct(properties),
            testsuite=[XUnitTFTestSuite.construct(test_suite) for test_suite in results.test_suites]
        )

    def to_xml_string(self, pretty_print: bool = False) -> str:
        return XmlSerializer(config=SerializerConfig(pretty_print=pretty_print), writer=XmlWriter).render(self)
