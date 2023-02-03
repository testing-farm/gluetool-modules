# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import pytest
import tempfile
import bs4
import logging

import gluetool
import gluetool_modules_framework.testing.test_schedule_report

from gluetool_modules_framework.libs.test_schedule import (
    TestSchedule, TestScheduleResult as TSResult, TestScheduleEntryStage as TSEntryStage,
    TestScheduleEntryState as TSEntryState
)

from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from gluetool_modules_framework.libs.guest_setup import GuestSetupStage, GuestSetupOutput, STAGES_ORDERED
from gluetool_modules_framework.testing.test_schedule_tmt import TestScheduleEntry
from gluetool_modules_framework.helpers.rules_engine import RulesEngine

from dataclasses import dataclass

from . import create_module, patch_shared


ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


@dataclass
class PrimaryTaskMock():
    id: int
    ARTIFACT_NAMESPACE: str


def read_asset_file(asset_filename: str):
    with open(os.path.join(ASSETS_DIR, 'test_schedule_report', asset_filename), 'r') as f:
        return f.read()


def read_xml_asset_file(asset_filename: str):
    xml = bs4.BeautifulSoup(read_asset_file(asset_filename), 'xml')
    # Remove the first line from the parsed assets file. BeautifulSoup adds '<?xml version="1.0" encoding="utf-8"?>'
    # to the first line when parsing a file.
    return '\n'.join(xml.prettify().splitlines()[1:])


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.testing.test_schedule_report.TestScheduleReport)[1]


def create_test_schedule(entry_properties):
    schedule = TestSchedule()
    for stage, state, result in entry_properties:
        entry = TestScheduleEntry(
            gluetool.log.Logging.get_logger(),
            TestingEnvironment(arch='x86_64', compose='Fedora37'),
            'plan',
            'repodir',
            ['exclude1']
        )
        entry.stage = stage
        entry.state = state
        entry.result = result
        schedule.append(entry)
    return schedule


@pytest.mark.parametrize('schedule, expected_schedule_result', [
    (
        create_test_schedule([
            (TSEntryStage.COMPLETE, None, None),
            (TSEntryStage.CREATED, None, None)
        ]),
        TSResult.UNDEFINED
    ),
    (
        create_test_schedule([
            (TSEntryStage.COMPLETE, TSEntryState.OK, None),
            (TSEntryStage.COMPLETE, TSEntryState.ERROR, None)
        ]),
        TSResult.ERROR
    ),
    (
        create_test_schedule([
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED)
        ]),
        TSResult.PASSED
    ),
    (
        create_test_schedule([
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.FAILED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.ERROR)
        ]),
        TSResult.FAILED
    ),
])
def test_overall_result_base(module, schedule: TestSchedule, expected_schedule_result: TSResult):
    module._overall_result(schedule)
    assert schedule.result == expected_schedule_result


@pytest.mark.parametrize('schedule, expected_schedule_result, overall_result_map', [
    (
        create_test_schedule([
            (TSEntryStage.COMPLETE, None, None),
            (TSEntryStage.CREATED, None, None)
        ]),
        TSResult.PASSED,
        'overall_result_map1.yaml'
    ),
])
def test_overall_result_custom(module, monkeypatch, schedule: TestSchedule, expected_schedule_result: TSResult, overall_result_map):

    module._config.update({
        'overall-result-map': os.path.join(ASSETS_DIR, 'test_schedule_report', overall_result_map)
    })
    rules_engine = create_module(RulesEngine)[1]
    patch_shared(monkeypatch, module, {}, callables={
        'eval_context': lambda: {},
        'evaluate_instructions': rules_engine.evaluate_instructions,
    })
    module._overall_result(schedule)

    assert schedule.result == expected_schedule_result


def test_execute(module, monkeypatch):
    with tempfile.TemporaryDirectory(prefix='test-schedule-report') as tmpdir:
        schedule = create_test_schedule([
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED)
        ])
        for i, entry in enumerate(schedule):
            entry.guest_setup_outputs = {
                stage: [
                    GuestSetupOutput(stage, 'schedule entry #{}: {}'.format(i, stage), 'logpath', 'some data')
                ] for stage in STAGES_ORDERED
            }

        patch_shared(monkeypatch, module, {}, callables={
            'test_schedule': lambda: schedule,
            'primary_task': lambda: PrimaryTaskMock(id=123456, ARTIFACT_NAMESPACE='SOME NAMESPACE'),
            'thread_id': lambda: 'some thread id'
        })
        module._config.update({
            'xunit-file': os.path.join(tmpdir, 'results.xml'),
            'enable-polarion': 1,
            'polarion-lookup-method': 'polarion lookup method',
            'polarion-lookup-method-field-id': 'polarion lookup method field id',
            'polarion-project-id': 'polarion project id',
        })
        module.execute()
        assert module.results().prettify() == read_xml_asset_file('results_execute.xml')
        assert module.test_schedule_results().prettify() == read_xml_asset_file('results_execute.xml')


def test_destroy(module, monkeypatch):
    with tempfile.TemporaryDirectory(prefix='test-schedule-report') as tmpdir:
        schedule = create_test_schedule([
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.PASSED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.FAILED),
            (TSEntryStage.COMPLETE, TSEntryState.OK, TSResult.ERROR)
        ])
        for i, entry in enumerate(schedule):
            entry.guest_setup_outputs = {
                stage: [
                    GuestSetupOutput(stage, 'schedule entry #{}: {}'.format(i, stage), 'logpath', 'some data')
                ] for stage in STAGES_ORDERED
            }

        patch_shared(monkeypatch, module, {}, callables={
            'test_schedule': lambda: schedule,
            'primary_task': lambda: PrimaryTaskMock(id=123456, ARTIFACT_NAMESPACE='SOME NAMESPACE'),
            'thread_id': lambda: 'some thread id'
        })
        module._config.update({
            'xunit-file': os.path.join(tmpdir, 'results.xml'),
            'enable-polarion': 1,
            'polarion-lookup-method': 'polarion lookup method',
            'polarion-lookup-method-field-id': 'polarion lookup method field id',
            'polarion-project-id': 'polarion project id',
        })

        module.destroy(failure=True)
        assert module.results().prettify() == read_xml_asset_file('results_destroy.xml')
        assert module.test_schedule_results().prettify() == read_xml_asset_file('results_destroy.xml')


@pytest.mark.parametrize('enable_polarion, polarion_project_id, polarion_lookup_method, polarion_lookup_method_field_id, expected_output', [  # noqa
    (True, None, None, None, (gluetool.GlueError, 'missing required options for Polarion.')),
    (True, None, 'project id', None, (gluetool.GlueError, 'missing required options for Polarion.')),
    (False, None, None, None, None),
    (False, 'id', None, None, (logging.WARN, "polarion options have no effect because 'enable-polarion' was not specified.")),  # noqa
])
def test_sanity(module, enable_polarion, polarion_project_id, polarion_lookup_method,
                polarion_lookup_method_field_id, expected_output, log):
    module._config.update({
        'enable-polarion': enable_polarion,
        'polarion-lookup-method': polarion_project_id,
        'polarion-lookup-method-field-id': polarion_lookup_method,
        'polarion-project-id': polarion_lookup_method_field_id
    })

    # Exception is expected
    if expected_output and expected_output[0] == gluetool.GlueError:
        with pytest.raises(expected_output[0], match=expected_output[1]):
            module.sanity()

    # Log message is expected
    elif expected_output:
        module.sanity()
        assert log.match(
            levelno=expected_output[0],
            message=expected_output[1]
        )
