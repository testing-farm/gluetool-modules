# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from gluetool_modules_framework.libs.test_schedule import TestScheduleEntryStage

from typing import Dict, List


# Global pipeline stages
GLOBAL_STAGES = ['initialization', 'test-discovery', 'testing', 'archiving']

# Per-plan stages by runner type
SINGLEHOST_PER_PLAN_STAGES = ['provisioning', 'guest-setup', 'running', 'cleanup']
MULTIHOST_PER_PLAN_STAGES = ['running']

RUNNER_PER_PLAN_STAGES: Dict[str, List[str]] = {
    'test-schedule-runner': SINGLEHOST_PER_PLAN_STAGES,
    'test-schedule-runner-multihost': MULTIHOST_PER_PLAN_STAGES,
}

# Pipeline-level event names
PIPELINE_INITIALIZATION_STARTED = 'pipeline.initialization-started'
PIPELINE_INITIALIZATION_FINISHED = 'pipeline.initialization-finished'
PIPELINE_TEST_DISCOVERY_STARTED = 'pipeline.test-discovery-started'
PIPELINE_TEST_DISCOVERY_FINISHED = 'pipeline.test-discovery-finished'
PIPELINE_TESTING_STARTED = 'pipeline.testing-started'
PIPELINE_TESTING_FINISHED = 'pipeline.testing-finished'
PIPELINE_ARCHIVING_STARTED = 'pipeline.archiving-started'
PIPELINE_ARCHIVING_FINISHED = 'pipeline.archiving-finished'

# Per-plan event names → stage name mapping
PER_PLAN_STAGE_EVENT_MAP: Dict[str, str] = {
    'test-schedule.provisioning-started': 'provisioning',
    'test-schedule.provisioning-finished': 'provisioning',
    'test-schedule.guest-setup-started': 'guest-setup',
    'test-schedule.guest-setup-finished': 'guest-setup',
    'test-schedule.running-started': 'running',
    'test-schedule.running-finished': 'running',
    'test-schedule.cleanup-started': 'cleanup',
    'test-schedule.cleanup-finished': 'cleanup',
}

# TestScheduleEntryStage → event names fired from _shift()
SINGLEHOST_STAGE_EVENTS: Dict[TestScheduleEntryStage, List[str]] = {
    TestScheduleEntryStage.GUEST_PROVISIONING: [
        'test-schedule.provisioning-started',
    ],
    TestScheduleEntryStage.GUEST_SETUP: [
        'test-schedule.provisioning-finished',
        'test-schedule.guest-setup-started',
    ],
    TestScheduleEntryStage.RUNNING: [
        'test-schedule.guest-setup-finished',
        'test-schedule.running-started',
    ],
    TestScheduleEntryStage.CLEANUP: [
        'test-schedule.running-finished',
        'test-schedule.cleanup-started',
    ],
    TestScheduleEntryStage.COMPLETE: [
        'test-schedule.cleanup-finished',
    ],
}

MULTIHOST_STAGE_EVENTS: Dict[TestScheduleEntryStage, List[str]] = {
    TestScheduleEntryStage.RUNNING: [
        'test-schedule.running-started',
    ],
    TestScheduleEntryStage.COMPLETE: [
        'test-schedule.running-finished',
    ],
}
