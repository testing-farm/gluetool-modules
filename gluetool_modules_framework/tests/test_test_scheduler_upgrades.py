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
    """
    Test if ``binary_rpms_list`` correctly works when compnent is not present.
    """
    assert rpms_list(module, monkeypatch, ['AppStream'], ['Python']) == []


def test_rpms_found_1repo(module, monkeypatch):
    assert rpms_list(module, monkeypatch, ['AppStream'], ['Box2D']) == ['Box2D']


def test_rpms_arch(module, monkeypatch):
    """
    Test that only x86_64 builds are considered.
    """
    assert rpms_list(module, monkeypatch, ['BaseOS'], ['ModemManager']) == []


def test_rpms_found_morerepos(module, monkeypatch):
    """
    Test if ``binary_rpms_list`` correctly works when no rpms in multiple repos are found.
    """
    assert rpms_list(module, monkeypatch, ['AppStream', 'CRB'], ['Box2D', 'CUnit', 'gcc']) ==\
        ['Box2D', 'CUnit-devel', 'gcc-plugin-devel', 'libstdc++-static']


def test_rpms_nopackages(module, monkeypatch):
    """
    Test if ``binary_rpms_list`` correctly works when no rpm is found.
    """
    assert rpms_list(module, monkeypatch, ['BaseOS'], ['gcc', 'bash']) == []


def test_rpms_default(module, monkeypatch):
    """
    Test if ``binary_rpms_list`` correctly works with default parameters (no rpms and repos excluded).
    """
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockResponse))
    assert module.binary_rpms_list('', ['Box2D', 'ModemManager', 'CUnit', 'gcc', 'glibc']) ==\
        ['Box2D', 'CUnit-devel', 'gcc-plugin-devel', 'libstdc++-static']


def test_rpms_exclude_repo(module, monkeypatch):
    """
    Test if 'exclude-repos' option correctly filters out rpms from given repository.
    """
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockResponse))
    module._config['exclude-repos'] = ['CRB']
    assert module.binary_rpms_list('', ['Box2D', 'ModemManager', 'CUnit', 'gcc', 'glibc']) ==\
        ['Box2D']


def test_rpms_nodebug(module, monkeypatch):
    """
    Test if debug rpms are excluded.
    """
    assert rpms_list(module, monkeypatch, ['CRB'], ['gcc']) == ['gcc-plugin-devel', 'libstdc++-static']


def test_rpms_nosource(module, monkeypatch):
    """
    Test if source rpms are excluded.
    """
    assert rpms_list(module, monkeypatch, ['AppStream'], ['Box2D']) == ['Box2D']


def test_rpms_nomodule(module, monkeypatch):
    """
    Test if modular builds are excluded.
    """
    assert rpms_list(module, monkeypatch, ['AppStream'], ['apache-commons-cli']) == ['apache-commons-cli']


def test_rpms_exclude_rpm(module, monkeypatch):
    """
    Test if 'exclude-rpms' option correctly filters out rpms based on plain string.
    """
    module._config['exclude-rpms'] = 'kernel-rt'
    assert rpms_list(module, monkeypatch, ['RT'], ['kernel']) == [
        'kernel-rt-core',
        'kernel-rt-debug',
        'kernel-rt-debug-core',
        'kernel-rt-debug-devel',
        'kernel-rt-debug-modules',
        'kernel-rt-debug-modules-core',
        'kernel-rt-debug-modules-extra',
        'kernel-rt-devel',
        'kernel-rt-modules',
        'kernel-rt-modules-core',
        'kernel-rt-modules-extra'
    ]


def test_rpms_exclude_rpm_re(module, monkeypatch):
    """
    Test if 'exclude-rpms' option correctly filters out rpms based on regex.
    """
    module._config['exclude-rpms'] = 'kernel-rt-debug.*'
    assert rpms_list(module, monkeypatch, ['RT'], ['kernel']) == [
        'kernel-rt',
        'kernel-rt-core',
        'kernel-rt-devel',
        'kernel-rt-modules',
        'kernel-rt-modules-core',
        'kernel-rt-modules-extra'
    ]


class MockFailedResponse:
    text = ''

    @staticmethod
    def raise_for_status():
        raise requests.exceptions.HTTPError


def test_rpms_get_failed(module, monkeypatch):
    """
    Test if ``binary_rpms_list`` raises exception when compose metadata is not found.
    """
    module._config['repos'] = ['AppStream', 'CRB', 'BaseOS']
    monkeypatch.setattr(requests, 'get', MagicMock(return_value=MockFailedResponse))
    with pytest.raises(GlueError, match=r'Unable to fetch compose metadata from: /metadata/rpms.json'):
        module.binary_rpms_list('', [])
