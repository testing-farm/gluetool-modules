# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections
import logging

import pytest
import requests
from mock import MagicMock

from gluetool_modules_framework.provision.artemis import ArtemisProvisioner, PipelineCancelled

from . import create_module, check_loadable


Response = collections.namedtuple('Response', ['status_code', 'text', 'json'])


class MockRequests(MagicMock):
    @staticmethod
    def get(url, json=None):
        if 'about' in url:
            return Response(200, '0.0.28', None)
        if url.endswith('guests/'):
            return Response(200, '', lambda: [])


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(ArtemisProvisioner)[1]

    module._config['api-url'] = "https://artemis.xyz"
    module._config['api-call-tick'] = 1
    module._config['api-call-timeout'] = 1

    return module


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/provision/artemis.py', 'ArtemisProvisioner')


def test_sanity_shared(module):
    for shared in ['provision', 'provisioner_capabilities']:
        assert module.glue.has_shared(shared) is True


def test_execute(monkeypatch, module, log):
    assert module.pipeline_cancelled == False

    monkeypatch.setattr(requests, 'get', MockRequests().get)
    module.execute()

    assert log.match(levelno=logging.INFO, message='Using Artemis API https://artemis.xyz/')


def test_pipeline_cancelled(module):
    module.glue.pipeline_cancelled = True

    with pytest.raises(PipelineCancelled):
        module.execute()
