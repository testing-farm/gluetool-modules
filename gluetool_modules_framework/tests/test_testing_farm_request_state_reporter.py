# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import pytest
import gluetool_modules_framework.testing_farm.testing_farm_request
import gluetool_modules_framework.helpers.testing_farm_request_state_reporter
import gluetool_modules_framework.helpers.rules_engine
import gluetool_modules_framework.libs.results

from gluetool import Failure, GlueError
from . import create_module, patch_shared

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'testing_farm')
EMPTY_EVAL_CONTEXT = {}


@pytest.fixture(name='module')
def fixture_module():
    return create_module(
        gluetool_modules_framework.helpers.testing_farm_request_state_reporter.TestingFarmRequestStateReporter
    )[1]


@pytest.fixture(name='request_empty')
def fixture_request_empty(module, monkeypatch):
    request = {
        'state': None,
        'overall_result': None,
        'xunit': None,
        'summary': None,
        'artifacts_url': None
    }
    patch_shared(monkeypatch, module, {
        'testing_farm_request': request,
    })


@pytest.fixture(name='request_running')
def fixture_request_running(module, monkeypatch):
    request = {'state': 'running', 'artifacts_url': None}
    patch_shared(monkeypatch, module, {
        'testing_farm_request': request,
    })


@pytest.fixture(name='results')
def fixture_result(module, monkeypatch):
    results = gluetool_modules_framework.libs.results.Results(
        overall_result='some-overall-result',
        test_schedule_result='some-test-schedule-result'
    )
    patch_shared(monkeypatch, module, {
        'results': results,
    })


@pytest.fixture(name='rules_engine')
def fixture_rules_engine():
    return create_module(gluetool_modules_framework.helpers.rules_engine.RulesEngine)[1]


@pytest.fixture(name='evaluate')
def fixture_evaluate(module, monkeypatch, rules_engine):
    module.glue.add_shared('evaluate_instructions', rules_engine)
    module.glue.add_shared('evaluate_rules', rules_engine)


@pytest.fixture(name='empty_eval_context')
def fixture_empty_eval_context(module, monkeypatch):
    patch_shared(monkeypatch, module, {
        'eval_context': EMPTY_EVAL_CONTEXT,
    })


def test_testing_farm_reporter_execute(module, request_empty):
    module.execute()
    request = module.shared('testing_farm_request')
    assert request == {
        'state': 'running',
        'overall_result': None,
        'xunit': None,
        'summary': None,
        'artifacts_url': None
    }


def test_testing_farm_reporter_destroy_failure_systemexit(module, request_empty):
    module.execute()
    module.destroy(failure=Failure(module, [None, SystemExit()]))
    request = module.shared('testing_farm_request')
    assert request == {
        'state': 'running',
        'overall_result': None,
        'xunit': None,
        'summary': None,
        'artifacts_url': None
    }


def test_testing_farm_reporter_destroy_failure(module, request_empty, evaluate):
    failure = Failure(module, [None, GlueError('message')])
    module.destroy(failure=failure)
    request = module.shared('testing_farm_request')
    assert request == {
        'state': 'error',
        'summary': str(GlueError('message')),
        'overall_result': 'unknown',
        'artifacts_url': None,
        'xunit': None,
        'destroying': True
    }


def test_testing_farm_reporter_destroy_failure_mapping(module, request_empty, evaluate, empty_eval_context):
    failure = Failure(module, [GlueError, GlueError('message')])

    module._config['state-map'] = os.path.join(ASSETS_DIR, 'state-map.yaml')
    module._config['overall-result-map'] = os.path.join(ASSETS_DIR, 'overall-result-map.yaml')

    module.destroy(failure=failure)
    request = module.shared('testing_farm_request')
    assert request == {
        'state': 'some-mapped-state',
        'summary': str(GlueError('message')),
        'overall_result': 'some-mapped-overall-result',
        'artifacts_url': None,
        'xunit': None,
        'destroying': True
    }


def test_testing_farm_reporter_destroy_no_result(module, request_running, evaluate):
    module.destroy()
    request = module.shared('testing_farm_request')
    assert request == {
        'state': 'complete',
        'overall_result': 'unknown',
        'xunit': None,
        'summary': None,
        'artifacts_url': None,
        'destroying': True
    }


def test_testing_farm_reporter_destroy_result(module, request_empty, results, evaluate):
    module.destroy()
    result = module.shared('testing_farm_request')
    assert result == {
        'state': 'complete',
        'overall_result': 'some-overall-result',
        'xunit': '<?xml version="1.0" encoding="UTF-8"?>\n<testsuites overall-result="some-overall-result"><properties><property name="baseosci.overall-result" value="some-test-schedule-result"/></properties></testsuites>',  # noqa
        'artifacts_url': None,
        'summary': None,
        'destroying': True
    }
