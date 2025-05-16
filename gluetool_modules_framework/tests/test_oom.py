# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import psutil
import pytest
import gluetool_modules_framework.helpers.oom
import tempfile
import time
from mock import MagicMock

from . import create_module


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(gluetool_modules_framework.helpers.oom.OutOfMemory)[1]

    module._config['enabled'] = True
    module._config['tick'] = 0.1
    module._config['monitoring-path'] = 'some-monitoring-path'
    module._config['verbose'] = True
    module._config['reservation'] = 1
    module._config['limit'] = 2

    return module


@pytest.fixture(name='monitoring_path')
def fixture_monitoring_path():
    with tempfile.NamedTemporaryFile(mode="w") as file:
        yield file


def test_required_options(module):
    assert module.required_options == ('reservation', 'limit')


def test_shared(module):
    assert module.glue.has_shared('oom_message')


def test_oom_unavailable(module, log):
    module._config['enabled'] = False

    module.execute()

    assert log.records[-1].message == 'Out-of-memory monitoring is disabled'
    assert log.records[-1].levelno == logging.INFO
    assert len(log.records) == 1


def test_monitoring_path_unavailable(module, log):
    module.execute()

    assert log.records[-1].message == "Out-of-memory monitoring is unavailable, 'some-monitoring-path' not available"
    assert log.records[-1].levelno == logging.WARNING
    assert len(log.records) == 1


@pytest.mark.parametrize(
    'memory_bytes,terminated', (
        (1024, False),
        (1024**2, False),
        (1.5*1024**2, False),
        (3*1024**2+1, True),
    ),
    ids=[
        'not-terminated',
        'not-terminated-1MiB',
        'reservation',
        'limit'
    ]
)
def test_oom_event(module, monitoring_path, monkeypatch, log, memory_bytes, terminated):
    module._config['monitoring-path'] = monitoring_path.name

    process_mock = MagicMock()
    monkeypatch.setattr(psutil, 'Process', process_mock)

    monitoring_path.write(str(memory_bytes))
    monitoring_path.flush()

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message.split('\n') == [
        "Starting out-of-memory monitoring, check every 0.1 seconds:",
        "{",
        '    "limit": "2 MiB",',
        '    "monitoring": "{}",'.format(monitoring_path.name),
        '    "reserved": "1 MiB"',
        "}",
    ]

    # make sure the timer runs
    time.sleep(0.5)

    if terminated:
        process_mock.assert_called_once()

    if terminated:
        assert module.oom_message() == "Worker out-of-memory, more than {} MiB consumed.".format(
            module._config['limit']
        )
    else:
        assert module.oom_message() is None


def test_oom_destroy(module, monitoring_path, monkeypatch, log):
    module._config['monitoring-path'] = monitoring_path.name

    process_mock = MagicMock()
    monkeypatch.setattr(psutil, 'Process', process_mock)

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message.split('\n') == [
        "Starting out-of-memory monitoring, check every 0.1 seconds:",
        "{",
        '    "limit": "2 MiB",',
        '    "monitoring": "{}",'.format(monitoring_path.name),
        '    "reserved": "1 MiB"',
        "}",
    ]

    monitoring_path.write(str(1024*3))
    monitoring_path.flush()

    module.destroy()

    assert process_mock.called_once()
    assert log.records[-1].message == 'Stopping out-of-memory monitoring'
