# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import logging

import pytest

import gluetool
import gluetool_modules_framework.infrastructure.copr
from gluetool_modules_framework.infrastructure.copr import Copr, TaskArches
from . import create_module, check_loadable

from mock import MagicMock

BUILD_INFO = {
    'chroots': [
        'fedora-28-x86_64'
    ],
    'ended_on': 1537775303,
    'id': 802020,
    'is_background': False,
    'ownername': 'mkluson',
    'project_dirname': 'pycho',
    'projectname': 'pycho',
    'repo_url': 'https://download.copr.fedorainfracloud.org/results/mkluson/pycho',
    'source_package': {
        'name': 'pycho',
        'url': 'https://copr.fedorainfracloud.org/tmp/tmpw7y826ay/pycho-0.84-1.fc27.src.rpm',
        'version': '0.84-1.fc27',
    },
    'started_on': 1537775025,
    'state': 'succeeded',
    'submitted_on': 1537775125,
    'submitter': 'mkluson'
}

BUILD_TASK_INFO_NOT_FOUND = {
    'error': 'Chroot fedora-38-x86_64 does not exist'
}

BUILD_INFO_NOT_FOUND = {
    'error': 'Build 999999 does not exist'
}

BUILD_TASK_INFO = {
    'ended_on': 1537775303,
    'name': 'fedora-28-x86_64',
    'result_url':
        'https://copr-be.cloud.fedoraproject.org/results/mkluson/pycho/fedora-28-x86_64/00802020-pycho/',
    'started_on': 1537775126,
    'state': 'succeeded'
}

BUILD_TASK_INFO_ERROR = {
    'error': 'Build task {} for build {} not found'
}

BUILDER_LIVE_LOG = '''
...
Checking for unpackaged file(s): /usr/lib/rpm/check-files /builddir/build/BUILDROOT/pycho-0.84-1.fc28.x86_64
Wrote: /builddir/build/RPMS/pycho-0.84-1.fc28.x86_64.rpm
Wrote: /builddir/build/SRPMS/pycho-0.84-1.fc28.x86_64.src.rpm
Executing(%clean): /bin/sh -e /var/tmp/rpm-tmp.esjg1H
+ umask 022
+ cd /builddir/build/BUILD
+ /usr/bin/rm -rf /builddir/build/BUILDROOT/pycho-0.84-1.fc28.x86_64
+ exit 0
Finish: rpmbuild pycho-0.84-1.fc28.src.rpm
INFO: chroot_scan: 3 files copied to /var/lib/copr-rpmbuild/results/chroot_scan
INFO: /var/lib/mock/802020-fedora-28-x86_64-1537775442.571937/root/var/log/dnf.log
/var/lib/mock/802020-fedora-28-x86_64-1537775442.571937/root/var/log/dnf.librepo.log
/var/lib/mock/802020-fedora-28-x86_64-1537775442.571937/root/var/log/dnf.rpm.log
Finish: build phase for pycho-0.84-1.fc28.src.rpm
INFO: Done(/var/lib/copr-rpmbuild/results/pycho-0.84-1.fc28.src.rpm) Config(child) 0 minutes 47 seconds
INFO: Results and/or logs in: /var/lib/copr-rpmbuild/results
INFO: Cleaning up build root ('cleanup_on_success=True')
Start: clean chroot
INFO: unmounting tmpfs.
Finish: clean chroot
Finish: run
'''


PROJECT_BUILDS = {
    'items': [
        {
            'chroots': [
                'fedora-28-x86_64',
                'epel-7-x86_64',
                'epel-8-x86_64',
            ],
            'ended_on': 1640047998,
            'id': 802020,
            'is_background': False,
            'ownername': 'mkluson',
            'project_dirname': 'pycho',
            'projectname': 'pycho',
            'repo_url': 'https://download.copr.fedorainfracloud.org/results/mkluson/pycho',
            'source_package': {
                'name': 'pycho',
                'url': 'https://copr.fedorainfracloud.org/tmp/tmpw7y826ay/pycho-0.84-1.fc27.src.rpm',
                'version': '0.84-1.fc27',
            },
            'started_on': 1640047883,
            'state': 'succeeded',
            'submitted_on': 1640047818,
            'submitter': 'mkluson'
        },
        {
            'chroots': [
                'fedora-35-x86_64',
                'epel-7-x86_64',
                'epel-8-x86_64',
                'fedora-rawhide-x86_64'
            ],
            'ended_on': 1639654676,
            'id': 3058549,
            'is_background': False,
            'ownername': 'mkluson',
            'project_dirname': 'pycho',
            'projectname': 'pycho',
            'repo_url': 'https://download.copr.fedorainfracloud.org/results/mkluson/pycho',
            'source_package': {
                'name': 'pycho',
                'url': 'https://download.copr.fedorainfracloud.org/results/mkluson/pycho/srpm-builds/03058549/pycho-0.84-1.fc32.src.rpm',
                'version': '0.84-1.fc32'
            },
            'started_on': 1639654581,
            'state': 'succeeded',
            'submitted_on': 1639654557,
            'submitter': 'optak'
        }
    ],
    'meta': {
        'limit': None,
        'offset': 0,
        'order': 'id',
        'order_type': 'ASC'
    }
}


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(Copr)[1]

    module._config['copr-web-url-template'] = 'dummy-web-url-{{ TASK.id }}'

    return module


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/infrastructure/copr.py', 'Copr')


def test_execute(module, monkeypatch):
    module._config['task-id'] = '802020:fedora-28-x86_64'

    class dummy_request(object):

        def __init__(self, source):
            self.source = source
            self.content = str(self.source)
            self.status_code = 200

        def json(self):
            return self.source

    def mocked_get(url):
        if 'api_3/build-chroot' in url:
            source = BUILD_TASK_INFO
        elif 'api_3/build/list' in url:
            source = PROJECT_BUILDS
        elif 'api_3/build' in url:
            source = BUILD_INFO
        elif 'builder-live.log' in url:
            source = BUILDER_LIVE_LOG

        return dummy_request(source)

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.requests, 'get', mocked_get)

    assert module.eval_context == {}

    module.execute()

    eval_context = module.eval_context
    primary_task = module.primary_task()

    assert eval_context['ARTIFACT_TYPE'] == 'copr-build'
    assert eval_context['BUILD_TARGET'] == primary_task.target
    assert eval_context['NVR'] == primary_task.nvr
    assert eval_context['PRIMARY_TASK'] == primary_task
    assert eval_context['TASKS'] == module.tasks()

    assert primary_task.id == '802020:fedora-28-x86_64'
    assert primary_task.dispatch_id == '802020:fedora-28-x86_64'
    assert primary_task.status == 'succeeded'
    assert primary_task.component == 'pycho'
    assert primary_task.target == 'fedora-28-x86_64'
    assert primary_task.nvr == 'pycho-0.84-1.fc27'
    assert primary_task.owner == 'mkluson'
    assert primary_task.project == 'pycho'
    assert primary_task.issuer == 'mkluson'
    assert primary_task.component_id == 'mkluson/pycho/pycho'

    assert primary_task.rpm_names == ['pycho-0.84-1.fc28.x86_64']
    assert primary_task.rpm_urls == [
        'https://copr-be.cloud.fedoraproject.org/results/mkluson/pycho/fedora-28-x86_64/00802020-pycho/pycho-0.84-1.fc28.x86_64.rpm']
    assert primary_task.srpm_urls == [
        'https://copr-be.cloud.fedoraproject.org/results/mkluson/pycho/fedora-28-x86_64/00802020-pycho/pycho-0.84-1.fc28.x86_64.src.rpm']

    assert primary_task.task_arches == TaskArches(['x86_64'])
    assert primary_task.full_name == "package 'pycho' build '802020' target 'fedora-28-x86_64'"

    assert primary_task.url == 'dummy-web-url-802020:fedora-28-x86_64'


@pytest.mark.parametrize(
    'build_info, error, raise_match',
    [
        (
            BUILD_INFO_NOT_FOUND,
            BUILD_INFO_NOT_FOUND['error'],
            r'^Error resolving copr build 999999:fedora-28-x86_64: {}$'.format(BUILD_INFO_NOT_FOUND['error'])
        ),
        (
            BUILD_INFO,
            BUILD_TASK_INFO_NOT_FOUND['error'],
            r'^Error resolving copr build 999999:fedora-28-x86_64: {}$'.format(BUILD_TASK_INFO_NOT_FOUND['error'])
        ),
    ],
    ids=('build_info_not_found', 'build_task_info_not_found')
)
def test_not_found(module, monkeypatch, build_info, error, raise_match):
    module._config['task-id'] = '999999:fedora-28-x86_64'

    class dummy_request(object):

        def __init__(self, source):
            self.source = source
            self.text = str(self.source)
            self.status_code = 200

        def json(self):
            return self.source

    def mocked_get(url):
        if 'api_3/build-chroot' in url:
            source = BUILD_TASK_INFO_NOT_FOUND
        elif 'api_3/build' in url:
            source = build_info

        return dummy_request(source)

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.requests, 'get', mocked_get)

    with pytest.raises(gluetool.GlueError, match=raise_match):
        module.execute()

    primary_task = module.primary_task()

    assert primary_task.error == error

    if build_info == BUILD_INFO:
        assert primary_task.component == 'pycho'
        assert primary_task.nvr == 'pycho-0.84-1.fc27'
        assert primary_task.owner == 'mkluson'
        assert primary_task.project == 'pycho'
        assert primary_task.issuer == 'mkluson'
        assert primary_task.component_id == 'mkluson/pycho/pycho'
    else:
        assert primary_task.component == 'UNKNOWN-COPR-COMPONENT'
        assert primary_task.nvr == 'UNKNOWN-COPR-COMPONENT-UNKNOWN-COPR-VERSION'
        assert primary_task.owner == 'UNKNOWN-COPR-OWNER'
        assert primary_task.project == 'UNKNOWN-COPR-PROJECT'
        assert primary_task.issuer == 'UNKNOWN-COPR-ISSUER'
        assert primary_task.component_id == 'UNKNOWN-COPR-OWNER/UNKNOWN-COPR-PROJECT/UNKNOWN-COPR-COMPONENT'

    assert primary_task.status == 'UNKNOWN-COPR-STATUS'
    assert primary_task.target == 'fedora-28-x86_64'

    assert primary_task.rpm_names == []
    assert primary_task.rpm_urls == []

    assert primary_task.task_arches == TaskArches(['x86_64'])

    assert primary_task.url == 'dummy-web-url-999999:fedora-28-x86_64'


def test_unreachable_copr(module, monkeypatch):
    module._config['task-id'] = '999999:fedora-28-x86_64'

    def api_mock(url):
        raise Exception

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.CoprApi, '_api_request', api_mock)

    with pytest.raises(gluetool.GlueError, match=r"Unable to get: api_3/build/999999"):
        module.execute()


def test_invalid_copr_get(module, monkeypatch):
    def mocked_get(url):
        raise Exception

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.requests, 'get', mocked_get)

    with pytest.raises(gluetool.GlueError,
                       match=r"^Invalid copr build with id '8020fedora-28-x86_64', must be 'build_id:chroot_name'"):
        module.tasks(task_ids=['8020fedora-28-x86_64'])


def test_invalid_copr_get_error(module, monkeypatch):
    def mocked_get(url):
        return MagicMock(status_code=200, json=lambda: {'error': 'mocked copr error'})

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.requests, 'get', mocked_get)

    with pytest.raises(
        gluetool.GlueError,
        match=r"^Error initializing copr task 5788940:fedora-38-aarch6: mocked copr error"
    ):
        module.tasks(task_ids=['5788940:fedora-38-aarch6'])


def test_tasks(module, monkeypatch):
    class dummy_request(object):

        def __init__(self, source):
            self.source = source
            self.text = str(self.source)
            self.status_code = 200

        def json(self):
            return self.source

    def mocked_get(url):
        if 'api_3/build-chroot' in url:
            source = BUILD_TASK_INFO
        elif 'api_3/build/list' in url:
            source = PROJECT_BUILDS
        elif 'api_3/build' in url:
            source = BUILD_INFO
        elif 'builder-live.log' in url:
            source = BUILDER_LIVE_LOG

        return dummy_request(source)

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.requests, 'get', mocked_get)

    task_ids = ['802020:fedora-28-x86_64', '802020:fedora-29-x86_64']

    # turn off logging for the module, so we do not spoil the output with info messages
    # when threads involved, pytest does not correctly catch the output
    original_loglevel = module.logger.logger.logger.level
    try:
        module.logger.setLevel(logging.ERROR)

        futures = []

        # we need this function to be multithread safe, because it is called
        # like that in Testing Farm when setting up a guest
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            for _ in range(100):
                futures.append(executor.submit(module.tasks, task_ids=task_ids))

            _, pending = concurrent.futures.wait(futures)

            assert not pending

    finally:
        module.logger.setLevel(original_loglevel)

    for future in futures:
        assert len(future.result()) == len(task_ids)


def test_expired(module, monkeypatch):
    module._config['task-id'] = '802020:fedora-28-x86_64'

    class dummy_request(object):

        def __init__(self, source):
            self.source = source
            self.content = str(self.source)
            self.status_code = 200

        def json(self):
            return self.source

    def mocked_get(url):
        if 'api_3/build' in url:
            source = BUILD_INFO
        elif 'builder-live.log' in url:
            source = None

        return dummy_request(source)

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.copr.requests, 'get', mocked_get)

    with pytest.raises(gluetool.SoftGlueError, match=r"Error looking up rpm urls for 802020:fedora-28-x86_64, failed or expired build?"):
        module.execute()
