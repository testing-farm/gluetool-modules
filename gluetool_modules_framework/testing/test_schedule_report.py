# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
from gluetool.utils import new_xml_element

from gluetool_modules_framework.libs import sort_children
from gluetool_modules_framework.libs.artifacts import artifacts_location
import gluetool_modules_framework.libs.guest_setup
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleResult, TestScheduleEntryStage, \
    TestScheduleEntryState

from gluetool_modules_framework.infrastructure.koji_fedora import KojiTask
from gluetool_modules_framework.infrastructure.copr import CoprTask


from gluetool_modules_framework.libs.results import Results, TestSuite, Log

# Type annotations
from typing import cast, TYPE_CHECKING, Any, Dict, List, Optional, Union  # noqa

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
            'xunit-testing-farm-file': {
                'help': 'File to save Testing Farm xunit results into (default: %(default)s).',
                'action': 'store',
                'default': None,
                'metavar': 'FILE'
            },
            'xunit-file': {
                'help': 'File to save xunit results into (default: %(default)s).',
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:

        super(TestScheduleReport, self).__init__(*args, **kwargs)

        self._results: Optional[Results] = None

    def sanity(self) -> None:
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
    def _overall_result_instructions(self) -> List[Dict[str, Any]]:

        instructions: List[Dict[str, Any]] = []

        for filepath in gluetool.utils.normalize_path_option(self.option('overall-result-map')):
            instructions += gluetool.utils.load_yaml(filepath, logger=self.logger)

        return instructions

    @property
    def _schedule(self) -> TestSchedule:

        return cast(
            TestSchedule,
            self.shared('test_schedule') or []
        )

    def _overall_result_base(self, schedule: TestSchedule) -> None:
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

    def _overall_result_custom(self, schedule: TestSchedule) -> None:
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

        def _set_result(instruction: Dict[str, Any], command: str, argument: str, context: Dict[str, Any]) -> None:

            result_name = argument.upper()
            result_value = TestScheduleResult.__members__.get(result_name, None)

            if result_value is None:
                raise gluetool.GlueError("Unkown result '{}' requested by configuration".format(result_name))

            schedule.result = result_value

        self.shared('evaluate_instructions', self._overall_result_instructions, {
            'set-result': _set_result
        }, context=context)

    def _overall_result(self, schedule: TestSchedule) -> TestScheduleResult:

        self._overall_result_base(schedule)
        self.debug('base overall result: {}'.format(schedule.result))

        self._overall_result_custom(schedule)
        self.debug('custom overall result: {}'.format(schedule.result))

        return schedule.result

    def _report_final_result(self, schedule: TestSchedule) -> None:

        result = self._overall_result(schedule)

        if result == TestScheduleResult.PASSED:
            self.info('Result of testing: PASSED')

        elif result == TestScheduleResult.FAILED:
            self.error('Result of testing: FAILED')

        else:
            self.warn('Result of testing: {}'.format(result))

    def _serialize_results(self, schedule: TestSchedule) -> None:

        self._results = Results()
        self._results.overall_result = self._overall_result(schedule).name.lower()

        # TODO: More task types are possible, having a base class would be handy.
        # using `# noqa` because flake8 and coala are confused by the walrus operator
        # Ignore PEP8Bear
        if primary_task := cast(Union[KojiTask, CoprTask], self.shared('primary_task')):  # noqa: E203 E231 E701
            self._results.primary_task = primary_task

        self._results.test_schedule_result = schedule.result.name.lower()

        if self.shared('thread_id'):
            self._results.testing_thread = self.shared('thread_id')

        if self.option('enable-polarion'):
            self._results.polarion_lookup_method = self.option('polarion-lookup-method')
            self._results.polarion_custom_lookup_method_field_id = self.option('polarion-lookup-method-field-id')
            self._results.polarion_project_id = self.option('polarion-project-id')

        for schedule_entry in schedule:
            test_suite = TestSuite(
                name=schedule_entry.id,
                result=schedule_entry.result.name.lower(),
                properties={'baseosci.result': schedule_entry.result.name.lower()},
            )

            for stage in gluetool_modules_framework.libs.guest_setup.STAGES_ORDERED:
                for output in schedule_entry.guest_setup_outputs.get(stage, []):
                    test_suite.logs.append(Log(
                        name=output.label,
                        href=artifacts_location(self, output.log_path, logger=schedule_entry.logger),
                        schedule_stage='guest-setup',
                        guest_setup_stage=stage.name.lower()
                    ))

            self.shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)
            self._results.test_suites.append(test_suite)

        self.info('serialized xunit_testing_farm results:\n{}'.format(
            self._results.xunit_testing_farm.to_xml_string(pretty_print=True)
        ))
        self.info('serialized xunit results:\n{}'.format(self._results.xunit.to_xml_string(pretty_print=True)))

    def results(self) -> Optional[Results]:
        return self._results

    def test_schedule_results(self) -> Optional[Results]:
        return self._results

    def _generate_results(self) -> None:

        self._serialize_results(self._schedule)

        self._report_final_result(self._schedule)

        assert self._results is not None

        if self.option('xunit-testing-farm-file'):
            with open(gluetool.utils.normalize_path(self.option('xunit-testing-farm-file')), 'w') as f:
                f.write(self._results.xunit_testing_farm.to_xml_string(pretty_print=True))
                f.flush()

            self.info('results saved into {}'.format(self.option('xunit-testing-farm-file')))

        if self.option('xunit-file'):
            with open(gluetool.utils.normalize_path(self.option('xunit-file')), 'w') as f:
                f.write(self._results.xunit.to_xml_string(pretty_print=True))
                f.flush()

            self.info('results saved into {}'.format(self.option('xunit-file')))

    def execute(self) -> None:
        self.require_shared('test_schedule')
        self._schedule.log(self.info, label='finished schedule')

        self._generate_results()

    def destroy(self, failure: Optional[Any] = None) -> None:

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
