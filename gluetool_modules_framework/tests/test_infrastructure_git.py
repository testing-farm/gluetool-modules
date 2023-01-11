# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import collections

import pytest

from mock import MagicMock

import gluetool
from gluetool.log import Logging

import gluetool_modules_framework.infrastructure.git
from gluetool_modules_framework.infrastructure.git import Git
from gluetool_modules_framework.libs.git import RemoteGitRepository as GitRepository
from . import create_module, patch_shared, check_loadable

Response = collections.namedtuple('Response', ['status_code', 'content', 'text'])


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.infrastructure.git.Git)[1]


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/infrastructure/git.py', 'Git')


@pytest.fixture(name='dummy_repository')
def fixture_dummy_repository(module):
    return GitRepository(module, clone_url='some-clone-url', branch='some-branch')


@pytest.fixture(name='dummy_repository_path')
def fixture_dummy_repository_path(module):
    return GitRepository(
        module, clone_url='some-clone-url', branch='some-branch', path='some-path'
    )


def test_sanity_shared(module):
    assert module.glue.has_shared('dist_git_repository') is True
    assert module.glue.has_shared('git_repository') is True


def test_eval_context(module, dummy_repository, monkeypatch):
    monkeypatch.setattr(module, '_repository', dummy_repository)
    assert module.eval_context['GIT_REPOSITORY'] is dummy_repository


def test_clone_url_eval_context(module, monkeypatch):
    module._config['clone-url'] = '{{ some_jinja_template }} {{ some_other_template }}'
    patch_shared(monkeypatch, module, {'eval_context': {'some_jinja_template': 'foo', 'some_other_template': 'bar'}})
    clone_url = module.clone_url
    assert clone_url == 'foo bar'


def test_ref_eval_context(module, monkeypatch):
    module._config['ref'] = '{{ some_ref_template }}'
    patch_shared(monkeypatch, module, {'eval_context': {'some_ref_template': 'foo'}})
    ref = module.ref
    assert ref == 'foo'


def test_eval_context_recursion(module, monkeypatch):
    monkeypatch.setattr(gluetool_modules_framework.libs, 'is_recursion', MagicMock(return_value=True))
    assert module.eval_context == {}


def test_repository_persistance(module, dummy_repository):
    module._repository = dummy_repository

    assert module.dist_git_repository() is dummy_repository


def test_repository_path(module, dummy_repository_path):
    assert dummy_repository_path.path == 'some-path'

    with pytest.raises(
        gluetool.GlueError,
        match=r"^Clone path does not match initialized repository, misunderstood arguments?"
    ):
        dummy_repository_path.clone(Logging.get_logger(), path='other-path')


def test_execute(module):
    module._config['clone-url'] = 'some-clone-url'
    module._config['ref'] = 'some-ref'
    module.execute()
    assert module._repository.clone_url == "some-clone-url"
    assert module._repository.ref == "some-ref"
