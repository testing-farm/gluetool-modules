# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import datetime
import threading

import gluetool
from gluetool import Failure

from typing import Any, Dict, List, Optional

from gluetool_modules_framework.libs.pipeline_stages import (
    PER_PLAN_STAGE_EVENT_MAP,
    PIPELINE_ARCHIVING_FINISHED,
    PIPELINE_ARCHIVING_STARTED,
    PIPELINE_INITIALIZATION_FINISHED,
    PIPELINE_INITIALIZATION_STARTED,
    PIPELINE_TEST_DISCOVERY_FINISHED,
    PIPELINE_TEST_DISCOVERY_STARTED,
    PIPELINE_TESTING_FINISHED,
    PIPELINE_TESTING_STARTED,
    RUNNER_PER_PLAN_STAGES,
    SINGLEHOST_PER_PLAN_STAGES,
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class TestingFarmPipelineStageReporter(gluetool.Module):
    name = 'testing-farm-pipeline-stage-reporter'
    description = 'Reports pipeline and per-plan stage progression to Testing Farm.'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(TestingFarmPipelineStageReporter, self).__init__(*args, **kwargs)

        self._stages: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._request: Any = None
        self._artifacts_url: Optional[str] = None

    def _add_stage(
        self,
        stage: str,
        plan: Optional[str] = None,
        entry_id: Optional[str] = None,
        workdir: Optional[str] = None,
        status: str = 'pending',
        started: Optional[str] = None,
        finished: Optional[str] = None
    ) -> None:
        self._stages.append({
            'stage': stage,
            'plan': plan,
            'id': entry_id,
            'workdir': workdir,
            'status': status,
            'started': started,
            'finished': finished,
        })

    def _update_stage(
        self,
        stage: str,
        entry_id: Optional[str] = None,
        status: Optional[str] = None,
        started: Optional[str] = None,
        finished: Optional[str] = None
    ) -> None:
        for entry in self._stages:
            if entry['stage'] == stage and entry['id'] == entry_id:
                if status is not None:
                    entry['status'] = status
                if started is not None:
                    entry['started'] = started
                if finished is not None:
                    entry['finished'] = finished
                break

    def _snapshot_stages(self) -> List[Dict[str, Any]]:
        return [dict(s) for s in self._stages]

    def _send_update(self, snapshot: List[Dict[str, Any]]) -> None:
        try:
            # If request is not set, we can't update
            # Try to refetch it if it's empty
            if not self._request:
                self._request = self.shared('testing_farm_request')
                if not self._request:
                    return

            # Try to refetch artifacts url before sending updates,
            # but don't block on lack of it
            if not self._artifacts_url:
                self._artifacts_url = self.shared('coldstore_url')

            self._request.update(
                stages={'pipeline': snapshot},
                artifacts_url=self._artifacts_url
            )

        except Exception:
            self.warn('Failed to send stage update', sentry=True)

    def execute(self) -> None:
        self.require_shared('testing_farm_request')

        self._request = self.shared('testing_farm_request')
        self._artifacts_url = self.shared('coldstore_url')

        now = _now()

        self._add_stage('initialization', status='running', started=now)
        self._add_stage('test-discovery')
        self._add_stage('testing')
        self._add_stage('archiving')
        self._send_update(self._snapshot_stages())

        self.shared('trigger_event', PIPELINE_INITIALIZATION_STARTED)

        self.shared('register_event_handler',
                    PIPELINE_INITIALIZATION_FINISHED,
                    self._handle_initialization_finished)

        self.shared('register_event_handler',
                    PIPELINE_TEST_DISCOVERY_STARTED,
                    self._handle_test_discovery_started)

        self.shared('register_event_handler',
                    'test-schedule.start',
                    self._handle_schedule_start)

        self.shared('register_event_handler',
                    'test-schedule.finished',
                    self._handle_schedule_finished)

        self.shared('register_event_handler',
                    'test-schedule.error',
                    self._handle_schedule_error)

        for event_name in PER_PLAN_STAGE_EVENT_MAP:
            self.shared('register_event_handler',
                        event_name,
                        self._handle_per_plan_event)

        self.shared('register_event_handler',
                    PIPELINE_ARCHIVING_STARTED,
                    self._handle_archiving_started)

        self.shared('register_event_handler',
                    PIPELINE_ARCHIVING_FINISHED,
                    self._handle_archiving_finished)

    def _handle_initialization_finished(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._update_stage('initialization', status='finished', finished=_now())
                snapshot = self._snapshot_stages()
            self._send_update(snapshot)
        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_test_discovery_started(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._update_stage('test-discovery', status='running', started=_now())
                snapshot = self._snapshot_stages()
            self._send_update(snapshot)
        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_schedule_start(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                now = _now()

                # Close initialization if still running (fallback for schedulers
                # that don't fire pipeline.initialization-finished)
                init_entry = next(
                    (e for e in self._stages if e['stage'] == 'initialization' and e['status'] == 'running'),
                    None
                )
                if init_entry:
                    init_entry['status'] = 'finished'
                    init_entry['finished'] = now

                # Close test-discovery if it was opened
                disc_entry = next(
                    (e for e in self._stages if e['stage'] == 'test-discovery' and e['status'] == 'running'),
                    None
                )
                if disc_entry:
                    disc_entry['status'] = 'finished'
                    disc_entry['finished'] = now

                self._update_stage('testing', status='running', started=now)

                schedule = kwargs.get('schedule')

                # Add per-plan stages
                if schedule:
                    runner: str = kwargs.get('runner', '')
                    per_plan_stages = RUNNER_PER_PLAN_STAGES.get(runner, SINGLEHOST_PER_PLAN_STAGES)

                    for entry in schedule:
                        plan = getattr(entry, 'plan', None)
                        workdir = getattr(entry, 'work_dirpath', None)

                        for stage_name in per_plan_stages:
                            self._add_stage(stage_name, plan=plan, entry_id=entry.id, workdir=workdir)

                snapshot = self._snapshot_stages()

            self.shared('trigger_event', PIPELINE_TEST_DISCOVERY_FINISHED)
            self.shared('trigger_event', PIPELINE_TESTING_STARTED)
            self._send_update(snapshot)

        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_per_plan_event(self, event: str, **kwargs: Any) -> None:
        try:
            schedule_entry = kwargs.get('schedule_entry')

            if not schedule_entry:
                return

            stage_name = PER_PLAN_STAGE_EVENT_MAP.get(event)

            if not stage_name:
                return

            now = _now()

            with self._lock:
                if event.endswith('-started'):
                    self._update_stage(stage_name, entry_id=schedule_entry.id, status='running', started=now)
                elif event.endswith('-finished'):
                    self._update_stage(stage_name, entry_id=schedule_entry.id, status='finished', finished=now)

                snapshot = self._snapshot_stages()

            self._send_update(snapshot)

        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_schedule_finished(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._update_stage('testing', status='finished', finished=_now())
                snapshot = self._snapshot_stages()

            self.shared('trigger_event', PIPELINE_TESTING_FINISHED)
            self._send_update(snapshot)

        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_schedule_error(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                now = _now()

                for entry in self._stages:
                    if entry['status'] == 'running' and entry['id'] is not None:
                        entry['status'] = 'failed'
                        entry['finished'] = now

                self._update_stage('testing', status='finished', finished=now)
                snapshot = self._snapshot_stages()

            self._send_update(snapshot)

        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_archiving_started(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._update_stage('archiving', status='running', started=_now())
                snapshot = self._snapshot_stages()
            self._send_update(snapshot)
        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def _handle_archiving_finished(self, event: str, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._update_stage('archiving', status='finished', finished=_now())
                snapshot = self._snapshot_stages()
            self._send_update(snapshot)
        except Exception:
            self.warn('Failed to handle {}'.format(event), sentry=True)

    def destroy(self, failure: Optional[Failure] = None) -> None:
        if failure is not None and isinstance(failure.exc_info[1], SystemExit):
            return

        if not self._request:
            return

        with self._lock:
            now = _now()

            for entry in self._stages:
                if entry['status'] == 'running' and entry['id'] is not None:
                    entry['status'] = 'failed'
                    entry['finished'] = now

            testing_entry = next(
                (e for e in self._stages if e['stage'] == 'testing' and e['plan'] is None),
                None
            )

            if testing_entry and testing_entry['status'] == 'running':
                testing_entry['status'] = 'finished'
                testing_entry['finished'] = now

            snapshot = self._snapshot_stages()

        self._send_update(snapshot)
