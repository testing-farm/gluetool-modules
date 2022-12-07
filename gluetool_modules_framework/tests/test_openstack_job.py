# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

import gluetool_modules_framework.testing.openstack.openstack_job

from gluetool_modules_framework.tests.test_dispatch_job import create_build_params

from gluetool_modules_framework.tests import create_module, check_loadable


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.testing.openstack.openstack_job.OpenStackJob)


def create_openstack_build_params(mod, **kwargs):
    params = {
        'ansible_options': 'some ansible options',
        'build_dependencies_options': 'some build-dependencies options',
        'install_mbs_build_options': 'some install mbs build options',
        'guess_environment_options': 'some guess-environment options',
        'install_brew_build_options': None,
        'wow_options': [
            'some w-t options',
            'other w-t options'
        ],
        'openstack_options': 'some openstack options',
        'artemis_options': 'some artemis options',
        'test_scheduler_options': 'some scheduler options',
        'test_scheduler_sti_options': 'some sti scheduler options',
        'test_schedule_tmt_options': 'some tmt scheduler options',
        'test_scheduler_upgrades_options': 'some upgrades scheduler options',
        'test_schedule_runner_options': 'some test-schedule-runner options',
        'test_schedule_runner_restraint_options': 'some test-schedule-runner-restraint options',
        'brew_build_task_params_options': 'some brew-build options',
        'brew_options': None,
        'dist_git_options': 'some dist-git options',
        'pipeline_install_ancestors_options': 'some pipeline-install-ancestors options',
        'github_options': 'some github options',
        'compose_url_options': 'some compose-url options',
        'wow_module_options': ''
    }

    params.update(kwargs)

    params = create_build_params(mod, **params)

    for arch in mod._config.get('with-arch'):
        params['test_scheduler_options'] = '{} --with-arch={}'.format(params['test_scheduler_options'], arch)

    for arch in mod._config.get('without-arch'):
        params['test_scheduler_options'] = '{} --without-arch={}'.format(params['test_scheduler_options'], arch)

    if mod._config.get('install-rpms-blacklist', None):
        params['brew_build_task_params_options'] = '{} --install-rpms-blacklist={}'.format(
            params['brew_build_task_params_options'], mod._config['install-rpms-blacklist'])

    params['wow_options'] = gluetool_modules_framework.testing.openstack.openstack_job.DEFAULT_WOW_OPTIONS_SEPARATOR.join(
        params['wow_options'])

    return params


def test_sanity(module):
    pass


def test_loadable(module):
    glue, _ = module

    check_loadable(glue, 'gluetool_modules_framework/testing/openstack/openstack_job.py', 'OpenStackJob')


@pytest.mark.parametrize('rpm_blacklist', [
    None,
    'blacklisted packages'
])
def test_build_params(module_with_primary_task, rpm_blacklist):
    mod = module_with_primary_task

    mod._config.update({
        'install-rpms-blacklist': rpm_blacklist,
        'wow-options-separator': gluetool_modules_framework.testing.openstack.openstack_job.DEFAULT_WOW_OPTIONS_SEPARATOR,
        'dist-git-options': 'some dist-git options',
        'install-mbs-build-options': 'some install mbs build options',
        'with-arch': ['arch-foo', 'arch-bar'],
        'without-arch': ['arch-baz']
    })

    expected_params = create_openstack_build_params(mod)

    assert mod.build_params == expected_params


def test_build_params_use_general_test_plan(module_with_primary_task):
    mod = module_with_primary_task

    mod._config.update({
        'wow-options-separator': gluetool_modules_framework.testing.openstack.openstack_job.DEFAULT_WOW_OPTIONS_SEPARATOR,
        'use-general-test-plan': True,
        'with-arch': [],
        'without-arch': []
    })

    expected_params = create_openstack_build_params(mod, wow_module_options='--use-general-test-plan')

    assert mod.build_params == expected_params
