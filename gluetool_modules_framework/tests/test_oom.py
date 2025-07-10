# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import psutil
import pytest
import gluetool_modules_framework.helpers.oom
import time
import signal
from mock import MagicMock

from . import create_module


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(gluetool_modules_framework.helpers.oom.OutOfMemory)[1]

    module._config['enabled'] = True
    module._config['tick'] = 0.1
    module._config['verbose-logging'] = True
    module._config['reservation'] = 1
    module._config['limit'] = 2
    module._config['force'] = True
    module._config['count-once'] = ['special-process', 'other-process']

    return module


def test_required_options(module):
    assert module.required_options == ('reservation', 'limit')


def test_shared(module):
    assert module.glue.has_shared('oom_message')


@pytest.mark.parametrize('option,values,expected', (
    (
        "verbose-logging",
        (False, "no", "false"),
        False
    ),
    (
        "verbose-logging",
        (True, "true", "True"),
        True
    ),
    (
        "enabled",
        (False, "no", "false"),
        False
    ),
    (
        "enabled",
        (True, "true", "True"),
        True
    ),
))
def test_options(option, values, expected):
    for value in values:
        module = create_module(gluetool_modules_framework.helpers.oom.OutOfMemory)[1]
        module._config[option] = value
        assert getattr(module, option.replace('-', '_')) == expected


@pytest.mark.parametrize('container,toolbox,available', (
    (True, True, False),
    (True, False, True),
    (False, False, False),
))
def test_available(module, monkeypatch, container, toolbox, available):
    def exists(path):
        if path == '/run/.containerenv':
            return container
        if path == '/run/.toolboxenv':
            return toolbox

    monkeypatch.setattr(os.path, 'exists', exists)

    assert module.available == available


def test_oom_unavailable(module, log):
    module._config['enabled'] = False

    module.execute()

    assert log.records[-1].message == 'Out-of-memory monitoring is disabled'
    assert log.records[-1].levelno == logging.INFO
    assert len(log.records) == 1


def test_check_unavailable(module, log, monkeypatch):
    module._config['force'] = False
    monkeypatch.setattr(os.path, 'exists', MagicMock(return_value=False))

    module.execute()

    assert log.records[-1].message == "Out-of-memory monitoring is unavailable, not running in a container"
    assert log.records[-1].levelno == logging.WARNING
    assert len(log.records) == 1


def test_print_usage_only(module, log):
    module._config['print-usage-only'] = True

    module.execute()

    assert log.records[-1].message.startswith('Detected memory usage:')
    assert log.records[-1].levelno == logging.INFO
    assert len(log.records) == 2


@pytest.mark.parametrize(
    'memory_bytes,terminated', (
        (1024, False),
        (1024**2, False),
        (1.5*1024**2, False),
        (3*1024**2, True),
    ),
    ids=[
        'not-terminated',
        'not-terminated-1MiB',
        'reservation',
        'limit'
    ]
)
def test_oom_event(module, monkeypatch, log, memory_bytes, terminated):
    process_mock = MagicMock()
    send_signal = MagicMock()
    process_mock.send_signal = send_signal
    monkeypatch.setattr(psutil, 'Process', MagicMock(return_value=process_mock))
    monkeypatch.setattr(module, 'total_rss_memory', MagicMock(return_value=memory_bytes))

    # pipeline cancellation is started in execute
    module.execute()
    assert log.records[-1].message.split('\n') == [
        "Starting out-of-memory monitoring, check every 0.1 seconds:",
        "{",
        '    "limit": "2 MiB",',
        '    "reserved": "1 MiB"',
        "}",
    ]

    # make sure the timer runs
    time.sleep(1)

    if terminated:
        send_signal.assert_called_once_with(signal.SIGUSR2)

    if terminated:
        assert module.oom_message() == "Worker out-of-memory, more than {} MiB consumed.".format(
            module._config['limit']
        )
    else:
        assert module.oom_message() is None

    module.destroy()
    assert log.records[-1].message == 'Stopping out-of-memory monitoring'


def test_total_rss_memory(module, monkeypatch, log):
    pmock = MagicMock()
    memory_info_mock = MagicMock()
    memory_info_mock.rss = 100
    pmock.memory_info = MagicMock(return_value=memory_info_mock)

    pmock_only_once = MagicMock()
    memory_info_mock = MagicMock()
    memory_info_mock.rss = 1000
    pmock_only_once.memory_info = MagicMock(return_value=memory_info_mock)
    pmock_only_once.cmdline = MagicMock(return_value=['special-process', 'some-arg'])

    pmock_raise = MagicMock()
    pmock_raise.pid = 1
    pmock_raise.memory_info = MagicMock(side_effect=psutil.NoSuchProcess(1))

    monkeypatch.setattr(
        psutil,
        'process_iter',
        MagicMock(return_value=[pmock, pmock, pmock, pmock_raise, pmock_only_once, pmock_only_once])
    )

    assert module.total_rss_memory() == 1300
    assert log.records[-1].message == "Ignoring process '1', it is gone or inacessible"
    assert log.records[-1].levelno == logging.DEBUG
