# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

import gluetool_modules_framework.static_analysis.osh.osh_job
from gluetool_modules_framework.tests.test_dispatch_job import create_build_params
from gluetool_modules_framework.tests import create_module, check_loadable


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.static_analysis.osh.osh_job.OSHJob)


def test_sanity(module):
    pass


def test_loadable(module):
    glue, _ = module

    check_loadable(glue, 'gluetool_modules_framework/static_analysis/osh/osh_job.py', 'OSHJob')


def test_build_params(module_with_primary_task):
    mod = module_with_primary_task

    expected_params = create_build_params(mod)

    assert mod.build_params == expected_params
