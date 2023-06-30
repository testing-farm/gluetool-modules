# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import attrs

from xsdata_attrs.bindings import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig
from xsdata.formats.dataclass.serializers.writers import XmlEventWriter

from typing import List, Optional, Dict, TYPE_CHECKING, cast

from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

if TYPE_CHECKING:
    from gluetool_modules_framework.libs.results import Results, TestSuite, TestCase, Log, Phase  # noqa


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFPackage:
    nvr: str = attrs.field(metadata={'type': 'Attribute'})


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFPackages:
    package: List[XUnitTFPackage]

    @classmethod
    def construct(cls, packages: List[str]) -> 'XUnitTFPackages':
        return XUnitTFPackages(package=[XUnitTFPackage(nvr=nvr) for nvr in sorted(packages)])


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFParameter:
    value: str = attrs.field(metadata={'type': 'Attribute'})


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFParameters:
    parameter: List[XUnitTFParameter]

    @classmethod
    def construct(cls, parameters: List[str]) -> 'XUnitTFParameters':
        return XUnitTFParameters(parameter=[XUnitTFParameter(value=value) for value in sorted(parameters)])


@attrs.define(kw_only=True)
class XUnitTFProperty:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    value: str = attrs.field(metadata={'type': 'Attribute'})


@attrs.define(kw_only=True)
class XUnitTFProperties:
    property: List[XUnitTFProperty]

    @classmethod
    def construct(cls, properties: Dict[str, str]) -> 'XUnitTFProperties':
        return XUnitTFProperties(
            property=[XUnitTFProperty(name=name, value=value) for name, value in sorted(properties.items())]
        )


@attrs.define(kw_only=True)
class XUnitTFLog:
    guest_setup_stage: Optional[str] = attrs.field(
        default=None,
        metadata={'type': 'Attribute', 'name': 'guest-setup-stage'}
    )
    href: str = attrs.field(metadata={'type': 'Attribute'})
    name: str = attrs.field(metadata={'type': 'Attribute'})
    schedule_entry: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute', 'name': 'schedule-entry'})
    schedule_stage: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute', 'name': 'schedule-stage'})


@attrs.define(kw_only=True)
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
        ) for log in sorted(logs, key=lambda log: log.name)])


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFPhase:
    logs: XUnitTFLogs
    name: str = attrs.field(metadata={'type': 'Attribute'})
    result: str = attrs.field(metadata={'type': 'Attribute'})
    time: Optional[str] = attrs.field(metadata={'type': 'Attribute'})


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFPhases:
    phase: List[XUnitTFPhase]

    @classmethod
    def construct(cls, phases: List['Phase']) -> 'XUnitTFPhases':
        return XUnitTFPhases(phase=[XUnitTFPhase(
            logs=XUnitTFLogs.construct(phase.logs),
            name=phase.name,
            result=phase.result,
            time=phase.time
        ) for phase in phases])


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFTestOutput:
    message: str = attrs.field(metadata={'type': 'Attribute'})


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFTestOutputs:
    test_output: List[XUnitTFTestOutput] = attrs.field(factory=list, metadata={'name': 'test-output'})

    @classmethod
    def construct(self, test_outputs: List[str]) -> 'XUnitTFTestOutputs':
        return XUnitTFTestOutputs(test_output=[XUnitTFTestOutput(message=message) for message in test_outputs])


@attrs.define(kw_only=True)
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


# Used in BaseOS CI results
@attrs.define(kw_only=True)
class XUnitTFFailure:
    message: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})


@attrs.define(kw_only=True)
class XUnitTFTestCase:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    result: Optional[str] = attrs.field(metadata={'type': 'Attribute'})
    time: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})  # Property used in BaseOS CI result

    properties: Optional[XUnitTFProperties]
    parameters: Optional[XUnitTFParameters] = None  # Property used in BaseOS CI results.xml
    logs: Optional[XUnitTFLogs]
    phases: Optional[XUnitTFPhases] = None  # Property used in BaseOS CI results.xml
    packages: Optional[XUnitTFPackages] = None  # Property used in BaseOS CI results.xml
    failure: Optional[XUnitTFFailure] = None
    error: Optional[XUnitTFFailure] = None
    testing_environment: List[XUnitTFTestingEnvironment] = attrs.field(
        factory=list,
        metadata={'name': 'testing-environment'}
    )
    test_outputs: Optional[XUnitTFTestOutputs] = attrs.field(default=None, metadata={'name': 'test-outputs'})

    # Properties used in BaseOS CI covscan module
    added: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})
    fixed: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})
    baseline: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})
    result_class: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})
    test_type: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})
    defects: Optional[str] = attrs.field(default=None, metadata={'type': 'Attribute'})

    @classmethod
    def construct(cls, test_case: 'TestCase') -> 'XUnitTFTestCase':
        environments: List[XUnitTFTestingEnvironment] = []
        if test_case.requested_environment:
            environments.append(XUnitTFTestingEnvironment.construct(test_case.requested_environment, 'requested'))
        if test_case.provisioned_environment:
            environments.append(XUnitTFTestingEnvironment.construct(test_case.provisioned_environment, 'provisioned'))

        return XUnitTFTestCase(
            name=test_case.name,
            result=test_case.result.value if test_case.result else None,
            properties=XUnitTFProperties.construct(test_case.properties) if test_case.properties else None,
            logs=XUnitTFLogs.construct(test_case.logs) if test_case.logs else None,
            testing_environment=environments,
            failure=XUnitTFFailure(message=test_case.failure if isinstance(test_case.failure, str) else None)
            if test_case.failure is not False else None,
            error=XUnitTFFailure(message=test_case.error if isinstance(test_case.error, str) else None)
            if test_case.error is not False else None,
            time=test_case.time,
            parameters=XUnitTFParameters.construct(test_case.parameters) if test_case.parameters else None,
            phases=XUnitTFPhases.construct(test_case.phases) if test_case.phases else None,
            # When `test_case.packages` is `None`, do not display the element, when it is `[]`, display <packages/>.
            packages=XUnitTFPackages.construct(test_case.packages) if test_case.packages is not None else None,
            test_outputs=XUnitTFTestOutputs.construct(test_case.test_outputs)
            if test_case.test_outputs is not None else None,
            added=test_case.added,
            fixed=test_case.fixed,
            baseline=test_case.baseline,
            result_class=test_case.result_class,
            test_type=test_case.test_type,
            defects=test_case.defects
        )


@attrs.define(kw_only=True)
class XUnitTFTestSuite:
    name: str = attrs.field(metadata={'type': 'Attribute'})
    result: Optional[str] = attrs.field(metadata={'type': 'Attribute'})
    tests: str = attrs.field(metadata={'type': 'Attribute'})
    logs: Optional[XUnitTFLogs] = None
    properties: Optional[XUnitTFProperties] = None
    testcase: List[XUnitTFTestCase] = attrs.field(factory=list)
    testing_environment: List[XUnitTFTestingEnvironment] = attrs.field(
        factory=list,
        metadata={'name': 'testing-environment'}
    )

    @classmethod
    def construct(cls, test_suite: 'TestSuite') -> 'XUnitTFTestSuite':
        environments: List[XUnitTFTestingEnvironment] = []
        if test_suite.requested_environment:
            environments.append(XUnitTFTestingEnvironment.construct(test_suite.requested_environment, 'requested'))
        if test_suite.provisioned_environment:
            environments.append(XUnitTFTestingEnvironment.construct(test_suite.provisioned_environment, 'provisioned'))

        return XUnitTFTestSuite(
            name=test_suite.name,
            result=test_suite.result.value if test_suite.result else None,
            tests=str(test_suite.test_count),
            logs=XUnitTFLogs.construct(test_suite.logs) if test_suite.logs else None,
            properties=XUnitTFProperties.construct(test_suite.properties) if test_suite.properties else None,
            testcase=[XUnitTFTestCase.construct(test_case) for test_case in test_suite.test_cases],
            testing_environment=environments
        )

    def to_xml_string(self, pretty_print: bool = False) -> str:
        return XmlSerializer(config=SerializerConfig(pretty_print=pretty_print)).render(self)


@attrs.define(kw_only=True)
class XUnitTFTestSuites:
    """
    Root element of Testing Farm xunit tree - data model representing the resulting xunit XML structure with ability to
    serialize itself from the knowledge of the know-it-all `Results` tree.
    """

    class Meta:
        name = 'testsuites'

    overall_result: Optional[str] = attrs.field(metadata={'type': 'Attribute', 'name': 'overall-result'})
    properties: Optional[XUnitTFProperties]
    testsuite: List[XUnitTFTestSuite]

    @classmethod
    def construct(cls, results: 'Results') -> 'XUnitTFTestSuites':
        properties: Dict[str, str] = {}
        if results.primary_task:
            properties.update({
                'baseosci.artifact-id': str(results.primary_task.id),
                'baseosci.artifact-namespace': results.primary_task.ARTIFACT_NAMESPACE
            })

        if results.test_schedule_result is not None:
            properties.update({'baseosci.overall-result': results.test_schedule_result.value})

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

        return XUnitTFTestSuites(
            overall_result=results.overall_result.value if results.overall_result else None,
            properties=XUnitTFProperties.construct(properties) if properties else None,
            testsuite=[XUnitTFTestSuite.construct(test_suite) for test_suite in results.test_suites]
        )

    def to_xml_string(self, pretty_print: bool = False) -> str:
        return XmlSerializer(
            config=SerializerConfig(pretty_print=pretty_print, pretty_print_indent=' '),
            writer=XmlEventWriter
        ).render(self)
