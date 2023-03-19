# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
from gluetool.utils import new_xml_element

from gluetool_modules_framework.libs import sort_children
from gluetool_modules_framework.libs.artifacts import artifacts_location
import gluetool_modules_framework.libs.guest_setup
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryStage, \
    TestScheduleEntryState
from lxml import objectify, etree
from junitparser import TestCase, TestSuite, JUnitXml, Skipped, Error

# Type annotations
from typing import cast, TYPE_CHECKING, Any, Dict, List, Optional  # noqa

if TYPE_CHECKING:
    import bs4  # noqa


class TestScheduleReport(gluetool.Module):
    """
    Report test results, carried by schedule entries, and prepare serialized version of these results
    in a form of xUnit document.

    Optionally, make the xunit polarion friendly.
    """

    name = 'test-schedule-report'

    options = [
        ('General Options', {
            'overall-result-map': {
                'help': """
                        Instructions for overruling the default decision on the overall schedule result
                        (default: none).
                        """,
                'action': 'append',
                'default': [],
                'metavar': 'FILE'
            },
            'xunit-file': {
                'help': 'File to save the results into, in an xUnit format (default: %(default)s).',
                'action': 'store',
                'default': None,
                'metavar': 'FILE'
            },
            'docs-link-reservation': {
                'help': """
                        A link to documentation of reservation workflow to display alongside the connection info.
                        (default: %(default)s).
                        """,
                'action': 'store',
                'default': None,
                'metavar': 'URL',
                'type': str
            }
        }),
        ('Polarion Options', {
            'enable-polarion': {
                'help': 'Make the xUnit RH Polarion friendly.',
                'action': 'store_true'
            },
            'polarion-lookup-method': {
                'help': 'Polarion lookup method.'
            },
            'polarion-lookup-method-field-id': {
                'help': 'Polarion lookup method field id.'
            },
            'polarion-project-id': {
                'help': 'Polarion project ID to use.'
            }
        })
    ]

    shared_functions = ['test_schedule_results', 'results']

    def __init__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None

        super(TestScheduleReport, self).__init__(*args, **kwargs)

        self._result = None  # type: bs4.element.Tag

    def sanity(self):
        # type: () -> None
        required_polarion_options = [
            'polarion-project-id',
            'polarion-lookup-method',
            'polarion-lookup-method-field-id'
        ]

        if self.option('enable-polarion') and not all(self.option(option) for option in required_polarion_options):
            raise gluetool.GlueError('missing required options for Polarion.')

        if not self.option('enable-polarion') and any(self.option(option) for option in required_polarion_options):
            self.warn("polarion options have no effect because 'enable-polarion' was not specified.")

    @gluetool.utils.cached_property
    def _overall_result_instructions(self):
        # type: () -> List[Dict[str, Any]]

        instructions = []  # type: List[Dict[str, Any]]

        for filepath in gluetool.utils.normalize_path_option(self.option('overall-result-map')):
            instructions += gluetool.utils.load_yaml(filepath, logger=self.logger)

        return instructions

    @property
    def _schedule(self):
        # type: () -> TestSchedule

        return cast(
            TestSchedule,
            self.shared('test_schedule') or []
        )

    def _overall_result_base(self, schedule):
        # type: (TestSchedule) -> None
        """
        Find out overall result of the schedule.

        1. if any entry is still incomplete, schedule result is ``UNDEFINED``
        2. if any entry finished didn't finish with ``OK`` state, schedule result is ``ERROR``
        3. if all entries finished with ``PASSED`` result, schedule result is ``PASSED``
        4. schedule result is the result of the first entry with non-``PASSED`` result
        """

        assert schedule

        if not all((schedule_entry.stage == TestScheduleEntryStage.COMPLETE for schedule_entry in schedule)):
            schedule.result = TestScheduleResult.UNDEFINED
            return

        if not all((schedule_entry.state == TestScheduleEntryState.OK for schedule_entry in schedule)):
            schedule.result = TestScheduleResult.ERROR
            return

        if all((schedule_entry.result == TestScheduleResult.PASSED for schedule_entry in schedule)):
            schedule.result = TestScheduleResult.PASSED
            return

        for schedule_entry in schedule:
            if not schedule_entry.result == TestScheduleResult.PASSED:
                schedule.result = schedule_entry.result
                return

    def _overall_result_custom(self, schedule):
        # type: (TestSchedule) -> None
        """
        Return overall result of the schedule, influenced by instructions provided by the user.
        """

        if not self._overall_result_instructions:
            return

        context = gluetool.utils.dict_update(
            self.shared('eval_context'),
            {
                'SCHEDULE': schedule,
                'CURRENT_RESULT': schedule.result,
                'Results': TestScheduleResult
            }
        )

        def _set_result(instruction, command, argument, context):
            # type: (Dict[str, Any], str, str, Dict[str, Any]) -> None

            result_name = argument.upper()
            result_value = TestScheduleResult.__members__.get(result_name, None)

            if result_value is None:
                raise gluetool.GlueError("Unkown result '{}' requested by configuration".format(result_name))

            schedule.result = result_value

        self.shared('evaluate_instructions', self._overall_result_instructions, {
            'set-result': _set_result
        }, context=context)

    def _overall_result(self, schedule):
        # type: (TestSchedule) -> TestScheduleResult

        self._overall_result_base(schedule)
        self.debug('base overall result: {}'.format(schedule.result))

        self._overall_result_custom(schedule)
        self.debug('custom overall result: {}'.format(schedule.result))

        return schedule.result

    def _report_final_result(self, schedule):
        # type: (TestSchedule) -> None

        result = self._overall_result(schedule)

        if result == TestScheduleResult.PASSED:
            self.info('Result of testing: PASSED')

        elif result == TestScheduleResult.FAILED:
            self.error('Result of testing: FAILED')

        else:
            self.warn('Result of testing: {}'.format(result))

    def _serialize_results(self, schedule):
        # type: (TestSchedule) -> None

        test_suites = gluetool.utils.new_xml_element('testsuites')
        test_suites['overall-result'] = self._overall_result(schedule).name.lower()

        testsuites_properties = gluetool.utils.new_xml_element('properties', _parent=test_suites)

        primary_task = self.shared('primary_task')
        if primary_task:
            gluetool.utils.new_xml_element(
                'property', _parent=testsuites_properties,
                name='baseosci.artifact-id', value=str(primary_task.id)
            )

            gluetool.utils.new_xml_element(
                'property', _parent=testsuites_properties,
                name='baseosci.artifact-namespace', value=primary_task.ARTIFACT_NAMESPACE
            )

        gluetool.utils.new_xml_element(
            'property', _parent=testsuites_properties,
            name='baseosci.overall-result', value=schedule.result.name.lower()
        )

        if self.shared('thread_id'):
            gluetool.utils.new_xml_element(
                'property', _parent=testsuites_properties,
                name='baseosci.id.testing-thread', value=self.shared('thread_id')
            )

        if self.option('enable-polarion'):
            # we use custom lookup method with Test Case ID as test id in Polarion
            gluetool.utils.new_xml_element(
                'property', _parent=testsuites_properties,
                name='polarion-lookup-method', value=self.option('polarion-lookup-method')
            )
            gluetool.utils.new_xml_element(
                'property', _parent=testsuites_properties,
                name='polarion-custom-lookup-method-field-id', value=self.option('polarion-lookup-method-field-id')
            )

            gluetool.utils.new_xml_element(
                'property', _parent=testsuites_properties,
                name='polarion-project-id', value=self.option('polarion-project-id')
            )

        sort_children(testsuites_properties, lambda child: child.attrs['name'])

        for schedule_entry in schedule:
            test_suite = gluetool.utils.new_xml_element(
                'testsuite',
                _parent=test_suites,
                name=schedule_entry.id,
                tests='0'
            )
            test_suite['result'] = schedule_entry.result.name.lower()
            test_suite['name'] = schedule_entry.id

            testsuite_logs = gluetool.utils.new_xml_element('logs', _parent=test_suite)
            testsuite_properties = gluetool.utils.new_xml_element('properties', _parent=test_suite)

            for stage in gluetool_modules_framework.libs.guest_setup.STAGES_ORDERED:
                outputs = schedule_entry.guest_setup_outputs.get(stage, [])

                for output in outputs:
                    new_xml_element(
                        'log',
                        _parent=testsuite_logs,
                        **{
                            'name': output.label,
                            'href': artifacts_location(self, output.log_path, logger=schedule_entry.logger),
                            'schedule-stage': 'guest-setup',
                            'guest-setup-stage': stage.name.lower()
                        }
                    )

            gluetool.utils.new_xml_element(
                'property', _parent=testsuite_properties,
                name='baseosci.result', value=schedule_entry.result.name.lower()
            )

            if hasattr(schedule_entry, 'result'):
                self.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

            sort_children(testsuite_properties, lambda child: child.attrs['name'])

        self._result = test_suites

        gluetool.log.log_xml(self.debug, 'serialized results', self._result)

    def results(self):
        # type: () -> bs4.element.Tag

        return self._result

    def test_schedule_results(self):
        # type: () -> bs4.element.Tag

        return self._result

    def _generate_results(self):
        # type: () -> None

        self._serialize_results(self._schedule)

        self._report_final_result(self._schedule)

        if self.option('xunit-file'):
            with open(gluetool.utils.normalize_path(self.option('xunit-file')), 'w') as f:
                f.write(gluetool.log.format_xml(self._result))
                f.flush()

            self.info('results saved into {}'.format(self.option('xunit-file')))

    def load_tf_xunit(tf_xunit_path):
        """Load TestingFarm XUnit file.
        :param tf_xunit_path: str, path to the TF Xunit file
        :return: str, content of the XUnit file
        """
        with open(tf_xunit_path) as f:
            # remove escaping which makes the file invalid for xml parsers
            tf_xunit = f.read().replace('\\"', '"')
        return tf_xunit

    def _generate_junit_results(self):
        # type: () -> None
        if self.option('xunit-file'):
            jxml = JUnitXml()

            tf_xunit = self.load_tf_xunit(gluetool.utils.normalize_path(self.option('xunit-file')))
            input_xml = objectify.fromstring(tf_xunit)
            for count, testsuite in enumerate(input_xml.testsuite):
                jxml.add_testsuite(testsuite)
                jxml.write('junit.xml')

    def execute(self):
        # type: () -> None
        self.require_shared('test_schedule')
        self._schedule.log(self.info, label='finished schedule')

        self._generate_results()
        self._generate_junit_results()

    def destroy(self, failure=None):
        # type: (Optional[Any]) -> None

        if not self._schedule:
            return

        if failure:
            self._schedule.log(self.info, label='aborted schedule')

            self._schedule.log(
                self.info,
                label='finished schedule',
                include_errors=True,
                include_logs=True,
                include_connection_info=True,
                connection_info_docs_link=self.option('docs-link-reservation'),
                module=self
            )

            self._generate_results()
            self._generate_junit_results()
