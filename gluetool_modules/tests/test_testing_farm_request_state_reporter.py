# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import gluetool_modules.testing_farm.testing_farm_request
import gluetool_modules.helpers.testing_farm_request_state_reporter

from gluetool import Failure, GlueError
from . import create_module, patch_shared


@pytest.fixture(name='module')
def fixture_module():
    return create_module(
        gluetool_modules.helpers.testing_farm_request_state_reporter.TestingFarmRequestStateReporter
    )[1]


@pytest.fixture(name='request_empty')
def fixture_request_empty(module, monkeypatch):
    request = {}
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
    results = {'overall-result': 'someresult'}
    patch_shared(monkeypatch, module, {
        'results': results,
    })


def test_testing_farm_reporter_execute(module, request_empty):
    module.execute()
    request = module.shared('testing_farm_request')
    assert request == {'state': 'running', 'artifacts_url': None}


def test_testing_farm_reporter_destroy_empty_request(module, request_empty):
    module.destroy()
    request = module.shared('testing_farm_request')
    assert request == {}


def test_testing_farm_reporter_destroy_failure_systemexit(module, request_running):
    failure = Failure(module, [None, SystemExit()])
    module.destroy(failure=failure)
    request = module.shared('testing_farm_request')
    assert request == {'state': 'running', 'artifacts_url': None}


def test_testing_farm_reporter_destroy_failure(module, request_running):
    failure = Failure(module, [None, GlueError('message')])
    module.destroy(failure=failure)
    request = module.shared('testing_farm_request')
    assert request == {
        'state': 'error',
        'summary': GlueError('message').message,
        'overall_result': 'error',
        'artifacts_url': None,
    }


def test_testing_farm_reporter_destroy_no_result(module, request_running):
    module.destroy()
    request = module.shared('testing_farm_request')
    assert request == {'state': 'error', 'overall_result': 'error', 'artifacts_url': None}


def test_testing_farm_reporter_destroy_result(module, request_running, results):
    module.destroy()
    result = module.shared('testing_farm_request')
    assert result == {
        'state': 'complete',
        'overall_result': 'someresult',
        'xunit': str({'overall-result': 'someresult'}),
        'artifacts_url': None,
    }
