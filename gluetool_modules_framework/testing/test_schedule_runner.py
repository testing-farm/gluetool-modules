# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import threading

import gluetool_modules_framework.libs.guest
import gluetool
from gluetool.action import Action
from gluetool.utils import normalize_bool_option, normalize_multistring_option, load_yaml, dict_update, GlueError
from gluetool.log import log_blob
from gluetool_modules_framework.libs.guest_setup import (
    GuestSetupStage, SetupGuestReturnType,
    STAGES_ORDERED as GUEST_SETUP_STAGES_ORDERED
)
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

# Get enum values of guest setup stages, containing the guest setup stage names
GUEST_SETUP_STAGES = [stage.value for stage in GUEST_SETUP_STAGES_ORDERED]


class TestScheduleRunner(gluetool.Module):
    """
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

    name = 'test-schedule-runner'
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
        'skip-guest-setup-stages': {
            'help': """
                    Skip given guest setup stages. Possible choices are {}.
                    """.format(', '.join(GUEST_SETUP_STAGES)),
            'choices': GUEST_SETUP_STAGES,
            'action': 'append',
            'metavar': 'STAGE'
        },
        'reuse-guests': {
            'help': "Reuse guests for running multiple tests",
            'action': 'store_true'
        },
        'destroy-if-fail': {
            'help': 'If the schedule entry fails, destroy its guest. This works only with the --reuse-guests option.',
            'action': 'store_true'
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:

        super(TestScheduleRunner, self).__init__(*args, **kwargs)

        self._test_schedule: TestSchedule = TestSchedule()

        self._guests_cache: List[gluetool_modules_framework.libs.guest.NetworkedGuest] = []
        self._guest_cache_lock = threading.Lock()

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

    @gluetool.utils.cached_property
    def skip_guest_setup_stages(self) -> List[str]:
        return normalize_multistring_option(self.option('skip-guest-setup-stages'))

    def sanity(self) -> None:
        if self.option('destroy-if-fail') and not self.option('reuse-guests'):
            raise GlueError('--destroy-if-fail option works only together with the --reuse-guests')

    def _get_entry_ready(self, schedule_entry: TestScheduleEntry) -> None:

        pass

    def _provision_guest(
        self,
        schedule_entry: TestScheduleEntry
    ) -> List[gluetool_modules_framework.libs.guest.NetworkedGuest]:

        # This is necessary - the output would tie the thread and the schedule entry in
        # the output. Modules used to actually provision the guest use their own module
        # loggers, therefore there's no connection between these two entities in the output
        # visible to the user with INFO+ log level.
        #
        # I don't like this line very much, it's way too similar to the most common next message:
        # usualy the ``provision`` shared function emits log message of form 'provisioning guest
        # for environment ...', but it's lesser of two evils. The proper solution would be propagation
        # of schedule_entry.logger down the stream for ``provision`` shared function to use. Leaving
        # that as an exercise for long winter evenings...
        schedule_entry.info('starting guest provisioning')

        with Action('provisioning guest', parent=schedule_entry.action, logger=schedule_entry.logger):
            return cast(
                List[gluetool_modules_framework.libs.guest.NetworkedGuest],
                self.shared('provision', schedule_entry.testing_environment, workdir=schedule_entry.work_dirpath)
            )

    def _setup_guest(self, schedule_entry: TestScheduleEntry) -> Any:

        schedule_entry.info('starting guest setup')

        def _run_setup(stage: GuestSetupStage) -> None:

            assert schedule_entry.guest is not None

            if stage.value in self.skip_guest_setup_stages:
                schedule_entry.warn("skip stage '{}' on user request".format(stage.value))
                return

            r_result = cast(
                SetupGuestReturnType,
                schedule_entry.guest.setup(stage=stage)
            )

            if r_result.is_ok:
                results, exc = r_result.unwrap(), None

            else:
                assert r_result.error is not None

                results, exc = r_result.error

            schedule_entry.guest_setup_outputs[stage] = results

            if results:
                schedule_entry.log_guest_setup_outputs(self, log_fn=schedule_entry.info)

            if not exc:
                return

            raise exc

        with Action(
            'pre-artifact-installation guest setup',
            parent=schedule_entry.action,
            logger=schedule_entry.logger
        ):
            _run_setup(GuestSetupStage.PRE_ARTIFACT_INSTALLATION)
            self.shared('generate_results', GuestSetupStage.PRE_ARTIFACT_INSTALLATION.value, generate_xunit=False)

        with Action(
            'pre-artifact-installation-workarounds guest setup',
            parent=schedule_entry.action,
            logger=schedule_entry.logger
        ):
            _run_setup(GuestSetupStage.PRE_ARTIFACT_INSTALLATION_WORKAROUNDS)
            self.shared(
                'generate_results', GuestSetupStage.PRE_ARTIFACT_INSTALLATION_WORKAROUNDS.value, generate_xunit=False)

        with Action(
            'artifact-installation guest setup',
            parent=schedule_entry.action,
            logger=schedule_entry.logger
        ):
            schedule_entry.info('installing the artifact')

            _run_setup(GuestSetupStage.ARTIFACT_INSTALLATION)
            self.shared('generate_results', GuestSetupStage.ARTIFACT_INSTALLATION.value, generate_xunit=False)

            schedule_entry.info('artifact installed')

        with Action(
            'post-artifact-installation-workarounds guest setup',
            parent=schedule_entry.action,
            logger=schedule_entry.logger
        ):
            _run_setup(GuestSetupStage.POST_ARTIFACT_INSTALLATION_WORKAROUNDS)
            self.shared(
                'generate_results', GuestSetupStage.POST_ARTIFACT_INSTALLATION_WORKAROUNDS.value, generate_xunit=False)

        with Action(
            'post-artifact-installation guest setup',
            parent=schedule_entry.action,
            logger=schedule_entry.logger
        ):
            _run_setup(GuestSetupStage.POST_ARTIFACT_INSTALLATION)
            self.shared('generate_results', GuestSetupStage.POST_ARTIFACT_INSTALLATION.value, generate_xunit=False)

    def _destroy_guest(self, schedule_entry: TestScheduleEntry) -> None:

        assert schedule_entry.guest is not None

        schedule_entry.info('starting destroying guest')

        with Action(
            'destroying guest',
            parent=schedule_entry.action,
            logger=schedule_entry.logger
        ):
            schedule_entry.guest.destroy()

    def _cleanup(self, schedule_entry: TestScheduleEntry) -> None:

        if self.option('reuse-guests'):
            assert schedule_entry.guest is not None

            if schedule_entry.result in [TestScheduleResult.FAILED, TestScheduleResult.ERROR]:

                if self.option('destroy-if-fail'):
                    self.info('The {} guest will be destroyed.'.format(schedule_entry.guest.name))
                    self._destroy_guest(schedule_entry)
                    return

            else:
                self._guests_cache.append(schedule_entry.guest)
                return

        self._destroy_guest(schedule_entry)

    def _run_tests(self, schedule_entry: TestScheduleEntry) -> None:

        schedule_entry.info('starting tests execution')

        with Action('test execution', parent=schedule_entry.action, logger=schedule_entry.logger):
            self.shared('run_test_schedule_entry', schedule_entry)

    def _find_cached_guest(
        self,
        schedule_entry: TestScheduleEntry
    ) -> Optional[gluetool_modules_framework.libs.guest.NetworkedGuest]:

        suitable_guest = None
        with self._guest_cache_lock:
            for guest in self._guests_cache:
                if schedule_entry.testing_environment == guest.environment:
                    self._guests_cache.remove(guest)
                    suitable_guest = guest

        # Check if suitable_guest is alive, destroy if not
        if suitable_guest:
            try:
                suitable_guest._wait_alive()
            except GlueError:
                self.warning('The guest is unavailable, destroy it and create a new one')
                suitable_guest.destroy()
                return None

        return suitable_guest

    def _destroy_cached_guests(self) -> None:
        for guest in self._guests_cache:
            with Action('destroying cached guest', parent=Action.current_action(), logger=self.logger):
                guest.destroy()

    def _run_schedule(self, schedule: TestSchedule) -> None:

        schedule_queue = (
            [entry for entry in schedule if entry.result != TestScheduleResult.SKIPPED]
            if not self.parallelize or self.parallel_limit else None
        )

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

            if schedule_entry.stage == TestScheduleEntryStage.READY:
                schedule_entry.debug('planning guest provisioning')

                _shift(schedule_entry, TestScheduleEntryStage.GUEST_PROVISIONING)

            elif schedule_entry.stage == TestScheduleEntryStage.GUEST_PROVISIONED:
                schedule_entry.debug('planning guest setup')

                _shift(schedule_entry, TestScheduleEntryStage.GUEST_SETUP)

            elif schedule_entry.stage == TestScheduleEntryStage.PREPARED:
                schedule_entry.info('planning test execution')

                _shift(schedule_entry, TestScheduleEntryStage.RUNNING)

                self.shared('generate_results', 'test execution started', generate_xunit=False)

        def _on_job_complete(result: Any, schedule_entry: TestScheduleEntry) -> None:

            if schedule_entry.stage == TestScheduleEntryStage.CREATED:
                schedule_entry.info('Entry is ready')

                # Try to find a suitable guest for reuse, preparation and run tests straight away
                if self.option('reuse-guests'):
                    guest = self._find_cached_guest(schedule_entry)
                    if guest:
                        schedule_entry.guest = guest
                        schedule_entry.info('cached guest suitable to entry is found')
                        _shift(schedule_entry, TestScheduleEntryStage.PREPARED)
                        engine.enqueue_jobs(_job(schedule_entry, 'running tests', self._run_tests))

                # Otherwise provision a guest
                if schedule_entry.guest is None:
                    _shift(schedule_entry, TestScheduleEntryStage.READY)
                    engine.enqueue_jobs(_job(schedule_entry, 'provisioning', self._provision_guest))

            elif schedule_entry.stage == TestScheduleEntryStage.GUEST_PROVISIONING:
                schedule_entry.info('guest provisioning finished')

                schedule_entry.guest = result[0]
                _shift(schedule_entry, TestScheduleEntryStage.GUEST_PROVISIONED)

                engine.enqueue_jobs(_job(schedule_entry, 'guest setup', self._setup_guest))

            elif schedule_entry.stage == TestScheduleEntryStage.GUEST_SETUP:
                schedule_entry.info('guest setup finished')

                schedule_entry.log_guest_setup_outputs(self, log_fn=schedule_entry.info)

                _shift(schedule_entry, TestScheduleEntryStage.PREPARED)

                engine.enqueue_jobs(_job(schedule_entry, 'running tests', self._run_tests))

            elif schedule_entry.stage == TestScheduleEntryStage.RUNNING:
                schedule_entry.info('test execution finished')

                # Here we should display "test logs are in ..." message like we do for guest-setup,
                # but leaving that for another patch as we don't have unified "report results"
                # structure yet.

                _shift(schedule_entry, TestScheduleEntryStage.CLEANUP)

                self.shared('generate_results', 'test execution finished', generate_xunit=False)

                engine.enqueue_jobs(_job(schedule_entry, 'cleanup', self._cleanup))

            elif schedule_entry.stage == TestScheduleEntryStage.CLEANUP:
                schedule_entry.info('cleanup finished')

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

            if schedule_entry.stage == TestScheduleEntryStage.GUEST_PROVISIONING:
                schedule_entry.error('guest provisioning failed: {}'.format(exc), exc_info=exc_info)

            elif schedule_entry.stage == TestScheduleEntryStage.GUEST_SETUP:
                schedule_entry.error('guest setup failed: {}'.format(exc), exc_info=exc_info)

                schedule_entry.log_guest_setup_outputs(self, log_fn=schedule_entry.info)

            elif schedule_entry.stage == TestScheduleEntryStage.RUNNING:
                schedule_entry.error('test execution failed: {}'.format(exc), exc_info=exc_info)

            elif schedule_entry.stage == TestScheduleEntryStage.CLEANUP:
                schedule_entry.error('cleanup failed: {}'.format(exc), exc_info=exc_info)

            if schedule_entry.stage in (
                TestScheduleEntryStage.GUEST_PROVISIONED, TestScheduleEntryStage.GUEST_SETUP,
                TestScheduleEntryStage.PREPARED, TestScheduleEntryStage.RUNNING
            ):
                self._cleanup(schedule_entry)

            _shift(schedule_entry, TestScheduleEntryStage.COMPLETE, new_state=TestScheduleEntryState.ERROR)

            _finish_action(schedule_entry)
            self.shared('generate_results', 'entry error', failure=exc, generate_xunit=False)

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

        if self.option('reuse-guests'):
            self._destroy_cached_guests()

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

        if self.option('reuse-guests'):
            self.info('Will reuse guests for schedule entries')

        with Action('executing test schedule', parent=Action.current_action(), logger=self.logger) as schedule.action:
            self._run_schedule(schedule)

            schedule.action.set_tag('result', schedule.result.name)

        self._test_schedule = TestSchedule()
