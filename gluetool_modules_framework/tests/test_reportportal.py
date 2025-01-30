# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import gluetool_modules_framework.testing_farm.testing_farm_request
import gluetool_modules_framework.helpers.reportportal
from mock import MagicMock
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module, check_loadable


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(gluetool_modules_framework.helpers.reportportal.ReportPortalModule)[1]
    return module


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/reportportal.py', 'ReportPortalModule')


@pytest.fixture(name='schedule')
def fixture_schedule_entry():
    schedule = []
    schedule_entry = MagicMock()
    schedule_entry.plan = 'mock-plan'
    schedule_entry.testing_environment = TestingEnvironment(
        compose='mock-compose',
        tmt={
            'environment': {
                'TMT_PLUGIN_REPORT_REPORTPORTAL_URL': 'reportportal.example.com',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_TOKEN': 'some-token',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_PROJECT': 'some-project',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN': '1'
            }}
    )
    schedule.append(schedule_entry)
    second_schedule_entry = MagicMock()
    second_schedule_entry.plan = 'mock-plan'
    second_schedule_entry.testing_environment = TestingEnvironment(
        compose='mock-compose',
        tmt={
            'environment': {
                'TMT_PLUGIN_REPORT_REPORTPORTAL_URL': 'reportportal.example.com',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_TOKEN': 'some-token',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_PROJECT': 'some-project',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN': '1'
            }}
    )
    schedule.append(second_schedule_entry)
    return schedule


@pytest.fixture(name='create_launch')
def fixture_create_launch(monkeypatch):
    create_launch_mock = MagicMock(return_value='12345')
    monkeypatch.setattr(gluetool_modules_framework.helpers.reportportal.ReportPortalAPI,
                        'create_launch', create_launch_mock)
    return create_launch_mock


@pytest.fixture(name='finish_launch')
def fixture_finish_launch(monkeypatch):
    finish_launch_mock = MagicMock()
    monkeypatch.setattr(gluetool_modules_framework.helpers.reportportal.ReportPortalAPI,
                        'finish_launch', finish_launch_mock)
    return finish_launch_mock


def test_reportportal_module(module, schedule, create_launch, finish_launch, log):

    launch_id = module.check_create_rp_launch(schedule[0])

    assert log.records[-2].message == 'Report Portal suite per plan is enabled'
    assert log.records[-1].message == 'Created Report Portal launch 12345'

    create_launch.assert_called_once()

    assert launch_id == '12345'

    module.destroy()
    finish_launch.assert_called_once()

    assert log.records[-1].message == 'Finalizing Report Portal launch 12345'


def test_multiple_same_environments(module, schedule, create_launch, finish_launch, log):

    launch_id1 = module.check_create_rp_launch(schedule[0])
    launch_id2 = module.check_create_rp_launch(schedule[1])

    assert create_launch.call_count == 1

    assert launch_id1 == '12345'
    assert launch_id2 == '12345'

    assert len(module.rp_api_launch_map) == 1

    module.destroy()

    assert finish_launch.call_count == 1


def test_multiple_different_environments(module, schedule, create_launch, finish_launch, log):

    schedule[1].testing_environment.tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_URL'] = 'reportportal2.example.com'
    launch_id1 = module.check_create_rp_launch(schedule[0])
    launch_id2 = module.check_create_rp_launch(schedule[1])

    assert create_launch.call_count == 2

    assert launch_id1 == '12345'
    assert launch_id2 == '12345'

    assert len(module.rp_api_launch_map) == 2

    module.destroy()

    assert finish_launch.call_count == 2


def test_bad_suite_per_plan_value(module, schedule, create_launch, finish_launch, log):
    schedule[0].testing_environment.tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN'] = '0'
    module.check_create_rp_launch(schedule[0])
    assert log.records[-1].message == 'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN=0 is not supported. Only value "1" enables the feature.'


def test_no_upload_to_launch(module, schedule, create_launch, finish_launch, log):
    schedule[0].testing_environment.tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_UPLOAD_TO_LAUNCH'] = '43223'
    module.check_create_rp_launch(schedule[0])
    assert log.records[-1].message == 'Report Portal launch is defined, a launch will not be created.'


def test_missing_variables(module, schedule, create_launch, finish_launch, log):
    schedule[0].testing_environment.tmt['environment'] = {}

    module.check_create_rp_launch(schedule[0])

    create_launch.assert_not_called()
