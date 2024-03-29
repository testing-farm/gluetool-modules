# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import requests

from mock import MagicMock

import gluetool
from gluetool import GlueError
from gluetool_modules_framework.testing.test_scheduler_upgrades import TestSchedulerUpgrades
from . import check_loadable, create_module, testing_asset
from typing import List


@pytest.fixture(name='module')
def fixture_module():
    return create_module(TestSchedulerUpgrades)[1]


def test_loadable(module):
    check_loadable(
        module.glue,
        'gluetool_modules_framework/testing/test_scheduler_upgrades.py',
        'TestSchedulerUpgrades')


def test_shared(module):
    assert module.glue.has_shared('create_test_schedule')


def test_sanity_from_fail(module):
    module._config['variant'] = 'from'
    module._config['destination'] = ''
    with pytest.raises(GlueError, match=r'Option `destination` is required when option `variant` is set to `from`.'):
        module.sanity()


def test_sanity_exclusive_fail(module):
    module._config['variant'] = 'to'
    module._config['repos'] = ['AppStream']
    module._config['exclude-repos'] = ['CRB']
    with pytest.raises(GlueError, match=r'Options `repos` and `exclude-repos` are mutually exclusive.'):
        module.sanity()


def test_sanity_pass(module):
    module._config['variant'] = 'from'
    module._config['destination'] = ' '
    module._config['product-template-pes'] = ' '
    module.sanity()


def test_product_version(module):
    product = 'rhel-7.9.z'
    module._config['product-pattern'] = '.*rhel-(?P<major>\d+)\.(?P<minor>\d+).*'
    assert module.product_version(product) == ('7', '9')


def test_pes_product(module):
    product = 'rhel-7.9.z'
    module._config['product-template-pes'] = 'RHEL {major}.{minor}'
    module.product_version = MagicMock(return_value=('7', '9'))
    assert module.format_for_pes(product) == 'RHEL 7.9'


def test_leapp_product(module):
    product = 'rhel-7.9.z'
    module._config['product-template-leapp'] = '{major}.{minor}'
    module.product_version = MagicMock(return_value=('7', '9'))
    assert module.format_for_leapp(product) == '7.9'


class MockResponse:
    text = ''

    @staticmethod
    def json():
        return gluetool.utils.load_json(testing_asset('test_scheduler_upgrades', 'compose.json'))

    @staticmethod
    def raise_for_status():
        return


def rpms_list(module, monkeypatch, repos: List[str], components: List[str]) -> List[str]:
    module._config['repos'] = repos
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockResponse))
    return module.binary_rpms_list('', components)


def test_rpms_component_not_found(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['AppStream'], ['Python']) == []


def test_rpms_found_1repo(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['AppStream'], ['Box2D']) == ['Box2D']


def test_rpms_arch(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['BaseOS'], ['ModemManager']) == []


def test_rpms_found_morerepos(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['AppStream', 'CRB'], ['Box2D', 'CUnit', 'gcc']) ==\
        ['Box2D', 'CUnit-devel', 'gcc-plugin-devel', 'libstdc++-static']


def test_rpms_nopackages(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['BaseOS'], ['gcc', 'bash']) == []


def test_rpms_default(module, monkeypatch):
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockResponse))
    assert module.binary_rpms_list('', ['Box2D', 'ModemManager', 'CUnit', 'gcc', 'glibc']) ==\
        ['Box2D', 'CUnit-devel', 'gcc-plugin-devel', 'libstdc++-static']


def test_rpms_exclude(module, monkeypatch):
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockResponse))
    module._config['exclude-repos'] = ['CRB']
    assert module.binary_rpms_list('', ['Box2D', 'ModemManager', 'CUnit', 'gcc', 'glibc']) ==\
        ['Box2D']


def test_rpms_nodebug(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['CRB'], ['gcc']) == ['gcc-plugin-devel', 'libstdc++-static']


def test_rpms_nosource(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['AppStream'], ['Box2D']) == ['Box2D']


def test_rpms_nomodule(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['AppStream'], ['apache-commons-cli']) == ['apache-commons-cli']


class MockFailedResponse:
    text = ''

    @staticmethod
    def raise_for_status():
        raise requests.exceptions.HTTPError


def test_rpms_get_failed(module, monkeypatch):
    module._config['repos'] = ['AppStream', 'CRB', 'BaseOS']
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockFailedResponse))
    with pytest.raises(GlueError, match=r'Unable to fetch compose metadata from: /metadata/rpms.json'):
        module.binary_rpms_list('', [])
