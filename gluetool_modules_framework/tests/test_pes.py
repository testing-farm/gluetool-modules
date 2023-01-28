# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import pytest
import requests
import simplejson

from mock import MagicMock

import gluetool
from gluetool_modules_framework.infrastructure import pes
from . import check_loadable, create_module, testing_asset


@pytest.fixture(name='module')
def fixture_module():

    module = create_module(pes.PES)[1]

    module._config['api-url'] = 'https://pes-api-url'
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 1

    return module


def prepare_test(module, monkeypatch, name, side_effect=None, side_effect_json=None):

    test = gluetool.utils.load_yaml(testing_asset('pes', 'test-{}.yaml'.format(name)))

    mocked_response = MagicMock(content='')

    if side_effect_json:
        mocked_response.json = MagicMock(side_effect=side_effect_json)
    else:
        mocked_response.json = MagicMock(return_value=gluetool.utils.load_json(testing_asset('pes', test['response'])))

    mocked_response.status_code = test['status_code']

    if side_effect:
        monkeypatch.setattr(requests, 'get', MagicMock(side_effect=side_effect))
        monkeypatch.setattr(requests, 'post', MagicMock(side_effect=side_effect))
    else:
        monkeypatch.setattr(requests, 'get', MagicMock(return_value=mocked_response))
        monkeypatch.setattr(requests, 'post', MagicMock(return_value=mocked_response))

    return (test, module)


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/infrastructure/pes.py', 'PES')


def test_shared(module):
    assert module.glue.has_shared('ancestor_components')
    assert module.glue.has_shared('successor_components')
    assert module.glue.has_shared('component_rpms')


@pytest.mark.parametrize('test', [
    'ancestors-no-events',
    'ancestors-multiple-events'
])
def test_ancestor_components(module, monkeypatch, test, log):

    (test, module) = prepare_test(module, monkeypatch, test)

    module.ancestor_components(test['package'], test['release'])

    assert log.match(
        message="Ancestors of component '{}' from target release '{}':\n{}".format(
            test['package'],
            test['release'],
            gluetool.log.format_dict(sorted(test['ancestors']))
        ), levelno=logging.INFO
    )


@pytest.mark.parametrize('test', [
    'successors-no-events',
    'successors-multiple-events'
])
def test_successors_components(module, monkeypatch, test, log):

    (test, module) = prepare_test(module, monkeypatch, test)

    module.successor_components(test['component'], test['initial_release'], test['release'])

    assert log.match(
        message="Successors of component '{}' ('{}') in release '{}':\n{}".format(
            test['component'],
            test['initial_release'],
            test['release'],
            gluetool.log.format_dict(sorted(test['successors']))
        ), levelno=logging.INFO
    )


def test_invalid_response(module, monkeypatch):

    (_, module) = prepare_test(module, monkeypatch, 'invalid-response')

    with pytest.raises(gluetool.GlueError, match=r'post.*returned 500'):
        module.ancestor_components('dummy package', 'dummy release')


def test_invalid_json(module, monkeypatch):

    exception = simplejson.errors.JSONDecodeError('', '', 0)

    (_, module) = prepare_test(module, monkeypatch, 'invalid-json', side_effect_json=exception)

    with pytest.raises(gluetool.GlueError, match=r'Pes returned unexpected non-json output, needs investigation'):
        module.ancestor_components('dummy package', 'dummy release')


def test_connection_error(module, monkeypatch):

    exception = requests.exceptions.ConnectionError('connection-error')
    (_, module) = prepare_test(module, monkeypatch, 'invalid-response', side_effect=exception)

    with pytest.raises(gluetool.GlueError, match=r"Condition 'getting post response from https://pes-api-url/srpm-events/' failed to pass within given time"):
        module.pes_api().get_ancestor_components('dummy package', 'dummy release')
