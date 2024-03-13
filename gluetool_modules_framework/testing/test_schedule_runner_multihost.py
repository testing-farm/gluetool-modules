# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0


import gluetool
from gluetool.action import Action
from gluetool.utils import normalize_bool_option, load_yaml, dict_update, GlueError
from gluetool.log import log_blob
from gluetool_modules_framework.libs.jobs import JobEngine, Job, handle_job_errors
from gluetool_modules_framework.libs.test_schedule import (
    TestScheduleEntryStage, TestScheduleEntryState, TestScheduleResult
)

# Type annotations
from typing import TYPE_CHECKING, cast, Any, Callable, Dict, List, Optional  # noqa
from gluetool_modules_framework.libs.test_schedule import TestSchedule, TestScheduleEntry, TestScheduleResult  # noqa

# Make sure all enums listed here use lower case values only, they will not work correctly with config file otherwise.
STRING_TO_ENUM = {
    'stage': TestScheduleEntryStage,
    'state': TestScheduleEntryState,
    'result': TestScheduleResult
}


class TestScheduleRunnerMultihost(gluetool.Module):
    """
    A copy of `testing.test_schedule_runner.TestScheduleRunner` module with modifications for multihost pipeline. It is
    intended to be used together with `testing.test_schedule_tmt_multihost.TestScheduleTMTMultihost`. The future plan
    is to merge multihost features from `TestScheduleRunnerMultihost` and `TestScheduleTMTMultihost` into their original
    counterparts.

    Original description of the `TestScheduleRunner` module:

    Dispatch tests, carried by schedule entries (`SE`), using runner plugins.

    For each `SE`, a shared function ``run_test_schedule_entry`` is called - this function is provided
    by one or more `runner plugins`, and takes care of executing tests prescribed by the `SE`. This module
    takes care of coordinating the work on the schedule level, updating states of `SEs` as necessary.
    It doesn't care about the tiny details like **how** to run the tests carried by `SE`.

    Runner plugins are expected to provide the shared function which accepts single `SE` and returns nothing.
    Plugin is responsible for updating ``SE.result`` attribute.

    SE execution is divided into several `stages`. Each stage is executed, when the previous one successfully finished.
    In special cases attributes of `SE` can be changed between stage transitions. Even `SE.stage` can be changed.
    Rules for such changes are held in config file specified by `--schedule-entry-attribute-map` option.

    The config file (in YAML format) is set of dictionaries. Each dictionary has to have rule stored under `rule` key.
    The rule is evaluated by `evaluate_filter` shared function, `eval_context` extend by the SE object is passed as a
    context for evaluation.

    Rest of the items is used as attributes of current SE, when the rule was met.

    Example of such config file:

    - rule: 'leapp-upgrade' in JENKINS_JOB_NAME and BUILD_TARGET.match(RHEL_8_0_0.ZStream.build_target.brew)
      stage: complete
      result: not_applicable
      results: []
    """

    name = 'test-schedule-runner-multihost'
    description = 'Dispatch tests, carried by schedule entries (`SE`), using runner plugins.'

    options = {
        'parallelize': {
            'help': 'Enable or disable parallelization of test schedule entries (default: %(default)s)',
            'default': 'no',
            'metavar': 'yes|no'
        },
        'parallel-limit': {
            'help': """
                Maximum number of entries running in parallel. Value is treated as a template. (default: %(default)s)
            """,
            'type': str,
            'default': '0',
            'metavar': 'NUMBER'
        },
        'max-parallel-limit': {
            'help': """
                Defines an upper bound for --parallel-limit option, which is user-defined.
            """,
            'type': int,
            'default': 64,
            'metavar': 'NUMBER'
        },
        'schedule-entry-attribute-map': {
            'help': """Path to file with schedule entry attributes and rules, when to use them. See modules's
                    docstring for more details. (default: %(default)s)""",
            'metavar': 'FILE',
            'type': str,
            'default': ''
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:

        super(TestScheduleRunnerMultihost, self).__init__(*args, **kwargs)

        self._test_schedule: TestSchedule = TestSchedule()

    @property
    def eval_context(self) -> Any:

        __content__ = {  # noqa
            'TEST_SCHEDULE': """
                             Current test schedule, or an empty list if no entries are available.
                             """
        }

        return {
            'TEST_SCHEDULE': self._test_schedule
        }

    @gluetool.utils.cached_property
    def parallelize(self) -> bool:

        return normalize_bool_option(self.option('parallelize'))

    @gluetool.utils.cached_property
    def parallel_limit(self) -> int:
        try:
            parallel_limit = int(gluetool.utils.render_template(
                self.option('parallel-limit'),
                **self.shared('eval_context')
            ))

            if not 0 < parallel_limit <= self.option('max-parallel-limit'):
                raise GlueError(
                    'parallel-limit must be an integer in the 1 to {} range'.format(self.option('max-parallel-limit'))
                )

            return parallel_limit

        except ValueError:
            raise GlueError("Could not convert 'parallel-limit' option value to integer")

    @gluetool.utils.cached_property
    def schedule_entry_attribute_map(self) -> Any:

        if not self.option('schedule-entry-attribute-map'):
            return []

        return load_yaml(self.option('schedule-entry-attribute-map'), logger=self.logger)

    def _get_entry_ready(self, schedule_entry: TestScheduleEntry) -> None:
        return

    def _run_tests(self, schedule_entry: TestScheduleEntry) -> None:

        schedule_entry.info('starting tests execution')

        with Action('test execution', parent=schedule_entry.action, logger=schedule_entry.logger):
            self.shared('run_test_schedule_entry', schedule_entry)

    def _run_schedule(self, schedule: TestSchedule) -> None:

        schedule_queue = schedule[:] if not self.parallelize or self.parallel_limit else None

        def _job(schedule_entry: TestScheduleEntry, name: str, target: Callable[[TestScheduleEntry], Any]) -> Job:

            return Job(
                logger=schedule_entry.logger,
                name='{}: {}'.format(schedule_entry.id, name),
                target=target,
                args=(schedule_entry,),
                kwargs={}
            )

        def _shift(schedule_entry: TestScheduleEntry,
                   new_stage: TestScheduleEntryStage,
                   new_state: Optional[TestScheduleEntryState] = None) -> None:

            old_stage, old_state = schedule_entry.stage, schedule_entry.state

            if new_state is None:
                new_state = old_state

            schedule_entry.stage, schedule_entry.state = new_stage, new_state

            schedule_entry.debug('shifted: {} => {}, {} => {}'.format(
                old_stage, new_stage, old_state, new_state
            ))

        def _set_action(schedule_entry: TestScheduleEntry) -> None:

            assert schedule_entry.testing_environment is not None

            schedule_entry.action = Action(
                'processing schedule entry',
                parent=schedule.action,
                logger=schedule_entry.logger,
                tags={
                    'entry-id': schedule_entry.id,
                    'runner-capability': schedule_entry.runner_capability,
                    'testing-environment': schedule_entry.testing_environment.serialize_to_json()
                }
            )

        def _finish_action(schedule_entry: TestScheduleEntry) -> None:

            assert schedule_entry.action is not None

            schedule_entry.action.set_tags({
                'stage': schedule_entry.stage.name,
                'state': schedule_entry.state.name,
                'result': schedule_entry.result.name
            })

            schedule_entry.action.finish()

        def _on_job_start(schedule_entry: TestScheduleEntry) -> None:
            schedule_entry.info('on start, stage: {}'.format(schedule_entry.stage))

            self.require_shared('evaluate_filter')

            filtered_items = self.shared(
                'evaluate_filter',
                self.schedule_entry_attribute_map,
                context=dict_update(
                    self.shared('eval_context'),
                    {'SCHEDULE_ENTRY': schedule_entry}
                )
            )

            if filtered_items:
                schedule_entry.warn('Schedule entry will be changed by config file')

            for item in filtered_items:
                for attribute, value in item.items():
                    if attribute == 'rule':
                        log_blob(self.debug, 'applied rule', value)
                        continue

                    if not hasattr(schedule_entry, attribute):
                        raise GlueError("Schedule entry has no attribute '{}'".format(attribute))

                    old_value = getattr(schedule_entry, attribute, None)

                    if attribute in STRING_TO_ENUM:
                        try:
                            new_value = STRING_TO_ENUM[attribute](value.lower())
                        except ValueError:
                            raise GlueError("Cannot set schedule entry {} to '{}'".format(attribute, value))
                    else:
                        new_value = value

                    setattr(schedule_entry, attribute, new_value)

                    schedule_entry.info(
                        '{} changed: {} => {}'.format(attribute, old_value, new_value)
                    )
            if schedule_entry.stage == TestScheduleEntryStage.PREPARED:
                schedule_entry.info('planning test execution')

                _shift(schedule_entry, TestScheduleEntryStage.RUNNING)

        def _on_job_complete(result: Any, schedule_entry: TestScheduleEntry) -> None:
            schedule_entry.info('on complete, stage: {}'.format(schedule_entry.stage))

            if schedule_entry.stage == TestScheduleEntryStage.CREATED:
                schedule_entry.info('Entry is prepared')

                _shift(schedule_entry, TestScheduleEntryStage.PREPARED)
                engine.enqueue_jobs(_job(schedule_entry, 'running tests', self._run_tests))

            elif schedule_entry.stage == TestScheduleEntryStage.RUNNING:
                schedule_entry.info('test execution finished')

                _shift(schedule_entry, TestScheduleEntryStage.COMPLETE)

                _finish_action(schedule_entry)

                # If parallelization is off, enqueue new entry
                if schedule_queue:
                    schedule_queue_entry = schedule_queue.pop(0)
                    _set_action(schedule_queue_entry)
                    engine.enqueue_jobs(_job(schedule_queue_entry, 'get entry ready', self._get_entry_ready))

        def _on_job_error(exc_info: Any, schedule_entry: TestScheduleEntry) -> None:

            schedule_entry.exceptions.append(exc_info)

            exc = exc_info[1]

            if schedule_entry.stage == TestScheduleEntryStage.RUNNING:
                schedule_entry.error('test execution failed: {}'.format(exc), exc_info=exc_info)

            elif schedule_entry.stage == TestScheduleEntryStage.CLEANUP:
                schedule_entry.error('cleanup failed: {}'.format(exc), exc_info=exc_info)

            _shift(schedule_entry, TestScheduleEntryStage.COMPLETE, new_state=TestScheduleEntryState.ERROR)

            _finish_action(schedule_entry)

            if schedule_queue:
                schedule_queue_entry = schedule_queue.pop(0)
                _set_action(schedule_queue_entry)
                engine.enqueue_jobs(_job(schedule_queue_entry, 'get entry ready', self._get_entry_ready))

        def _on_job_done(remaining_count: int, schedule_entry: TestScheduleEntry) -> None:

            # `remaining_count` is number of remaining jobs, but we're more interested in a number of remaining
            # schedule entries (one entry spawns multiple jobs, hence jobs are not useful to us).

            remaining_count = len([
                se for se in schedule if se.stage != TestScheduleEntryStage.COMPLETE
            ])

            schedule.log(self.info, label='{} entries pending'.format(remaining_count))

        self.shared('trigger_event', 'test-schedule.start',
                    schedule=schedule)

        schedule.log(self.info, label='running test schedule of {} entries'.format(len(schedule)))

        engine = JobEngine(
            logger=self.logger,
            on_job_start=_on_job_start,
            on_job_complete=_on_job_complete,
            on_job_error=_on_job_error,
            on_job_done=_on_job_done,
        )

        if self.parallelize:
            if self.parallel_limit:
                for _ in range(self.parallel_limit):

                    assert schedule_queue is not None

                    if not schedule_queue:
                        break

                    schedule_queue_entry = schedule_queue.pop(0)

                    assert schedule_queue_entry.testing_environment is not None

                    _set_action(schedule_queue_entry)

                    engine.enqueue_jobs(_job(schedule_queue_entry, 'get entry ready', self._get_entry_ready))

            else:
                for schedule_entry in schedule:
                    # We spawn new action for each schedule entry - we don't enter its context anywhere though!
                    # It serves only as a link between "schedule" action and "doing X to move entry forward" subactions,
                    # capturing lifetime of the schedule entry. It is then closed when we switch the entry to COMPLETE
                    # stage.

                    _set_action(schedule_entry)

                    engine.enqueue_jobs(_job(schedule_entry, 'get entry ready', self._get_entry_ready))
        else:

            if not schedule_queue:
                raise GlueError('no test schedule to run')

            schedule_queue_entry = schedule_queue.pop(0)

            assert schedule_queue_entry.testing_environment is not None

            _set_action(schedule_queue_entry)

            engine.enqueue_jobs(_job(schedule_queue_entry, 'get entry ready', self._get_entry_ready))

        engine.run()

        self._test_schedule.log(
            self.info,
            label='finished schedule',
            include_errors=True,
            include_logs=True,
            module=self
        )

        if engine.errors:
            self.shared('trigger_event', 'test-schedule.error',
                        schedule=schedule, errors=engine.errors)

            handle_job_errors(engine.errors, 'At least one entry crashed')

        self.shared('trigger_event', 'test-schedule.finished',
                    schedule=schedule)

    def execute(self) -> None:

        if normalize_bool_option(self.option('parallelize')):
            if self.option('parallel-limit'):
                self.info(
                    'Will run schedule entries in parallel, {} entries at once'.format(self.parallel_limit)
                )
            else:
                self.info('Will run schedule entries in parallel')

        else:
            self.info('Will run schedule entries serially')

        schedule = cast(
            TestSchedule,
            self.shared('test_schedule') or TestSchedule()
        )

        self._test_schedule = schedule

        with Action('executing test schedule', parent=Action.current_action(), logger=self.logger) as schedule.action:
            self._run_schedule(schedule)

            schedule.action.set_tag('result', schedule.result.name)

        self._test_schedule = TestSchedule()
