# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import gluetool_modules_framework.testing_farm.testing_farm_request
import gluetool_modules_framework.helpers.report_portal
from mock import MagicMock
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module, check_loadable


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(gluetool_modules_framework.helpers.report_portal.ReportPortalModule)[1]
    return module


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/report_portal.py', 'ReportPortalModule')


@pytest.fixture(name='tf_request')
def fixture_request():
    request = MagicMock()
    request.url = 'example.com'
    request.ref = '1234567890'
    request.environments_requested = [TestingEnvironment(
        tmt={
            'environment': {
                'TMT_PLUGIN_REPORT_REPORTPORTAL_URL': 'reportportal.example.com',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_TOKEN': 'some-token',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_PROJECT': 'some-project',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN': '1'
            }}
    )]
    return request


@pytest.fixture(name='create_launch')
def fixture_create_launch(monkeypatch):
    create_launch_mock = MagicMock(return_value='12345')
    monkeypatch.setattr(gluetool_modules_framework.helpers.report_portal.ReportPortalAPI,
                        'create_launch', create_launch_mock)
    return create_launch_mock


@pytest.fixture(name='finish_launch')
def fixture_finish_launch(monkeypatch):
    finish_launch_mock = MagicMock()
    monkeypatch.setattr(gluetool_modules_framework.helpers.report_portal.ReportPortalAPI,
                        'finish_launch', finish_launch_mock)
    return finish_launch_mock


def test_report_portal_module(module, tf_request, create_launch, finish_launch, log):

    module.check_create_rp_launch(tf_request)

    assert log.records[-2].message == 'Report Portal suite per plan is enabled'
    assert log.records[-1].message == 'Created Report Portal launch 12345'

    create_launch.assert_called_once()

    assert tf_request.environments_requested[0].tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_UPLOAD_TO_LAUNCH'] == '12345'

    module.destroy()
    finish_launch.assert_called_once()

    assert log.records[-1].message == 'Finalizing Report Portal launch 12345'


def test_multiple_environments(module, tf_request, create_launch, finish_launch, log):
    tf_request.environments_requested.append(TestingEnvironment(
        tmt={
            'environment': {
                'TMT_PLUGIN_REPORT_REPORTPORTAL_URL': 'reportportal2.example.com',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_TOKEN': 'some-other-token',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_PROJECT': 'some-other-project',
                'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN': '1'
            }}
    ))

    module.check_create_rp_launch(tf_request)

    assert create_launch.call_count == 2

    assert tf_request.environments_requested[0].tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_UPLOAD_TO_LAUNCH'] == '12345'
    assert tf_request.environments_requested[1].tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_UPLOAD_TO_LAUNCH'] == '12345'

    assert len(module.rp_api_launch_map) == 2

    module.destroy()

    assert finish_launch.call_count == 2


def test_bad_suite_per_plan_value(module, tf_request, create_launch, finish_launch, log):
    tf_request.environments_requested[0].tmt['environment']['TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN'] = '0'
    module.check_create_rp_launch(tf_request)
    assert log.records[-1].message == 'TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN=0 is not supported. Only value "1" enables the feature.'


def test_missing_variables(module, tf_request, create_launch, finish_launch, log):
    tf_request.environments_requested[0].tmt['environment'] = {}

    module.check_create_rp_launch(tf_request)

    create_launch.assert_not_called()
