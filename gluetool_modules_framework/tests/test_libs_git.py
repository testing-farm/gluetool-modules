# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import tempfile
import subprocess
from mock import MagicMock

import pytest

import git
from gluetool.log import Logging
import gluetool
import gluetool.utils
import gluetool_modules_framework.libs.git as git_module


@pytest.fixture(name='remote_git_repository')
def fixture_RemoteGitRepository():
    remote_git_repository = git_module.RemoteGitRepository(logger=Logging.get_logger())
    remote_git_repository.path = 'some-path'
    return remote_git_repository


@pytest.mark.parametrize('clone_url, branch, path, shallow_clone, ref, result', [
    ('clone_url_foo', 'branch_foo', 'path_foo', True, '', [
         '--depth', '1', '-b', 'branch_foo', 'clone_url_foo', 'path_foo'
    ]),
    ('clone_url', None, 'path', False, 'ref', [
         'clone_url', 'path'
    ]),
    ('clone_url_foo', 'branch_foo', 'path_foo', False, '', [
         '-b', 'branch_foo', 'clone_url_foo', 'path_foo'
    ])
])
def test_get_clone_options(clone_url, branch, path, shallow_clone, ref, result):
    remote_git_repository = git_module.RemoteGitRepository(
        logger=Logging.get_logger(),
        clone_url=clone_url,
        branch=branch,
        path=path,
        ref=ref
    )
    assert remote_git_repository._get_clone_options(
        clone_url=clone_url, branch=branch, path=path, shallow_clone=shallow_clone, ref=ref) == result


def test_invalid_clone_options(remote_git_repository):
    remote_git_repository.branch = "foo"
    remote_git_repository.ref = "foo"
    remote_git_repository.clone_url = "foo"
    with pytest.raises(gluetool.GlueError, match='Both ref and branch specified, misunderstood arguments?'):
        remote_git_repository.clone()


@pytest.mark.parametrize('path, prefix, expected_path, ref, branch', [
    ('some_path', '', 'some_path', '', 'branch_bar'),
    ('some_other_path', 'prefix_foo', 'some_other_path', 'ref_foo', ''),
    ('', 'foo', 'workdir', '', 'branch_bar'),
    ('', '', 'workdir', '', 'branch_bar')
])
def test_clone(remote_git_repository, path, prefix, ref, monkeypatch, log, expected_path, branch):
    remote_git_repository.clone_url = 'clone-url'
    remote_git_repository.path = path

    monkeypatch.setattr(os, 'chmod', MagicMock())

    def mock_command_run(_):
        return MagicMock(stdout=ref)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    mock_mkdtemp = MagicMock(return_value='workdir')
    monkeypatch.setattr(tempfile, 'mkdtemp', mock_mkdtemp)

    remote_git_repository.clone(path=path, prefix=prefix, ref=ref, branch=branch, clone_timeout=2, clone_tick=1)

    if ref:
        assert log.match(
            levelno=logging.INFO,
            message="cloning repo clone-url (branch not specified, ref {})".format(ref)
        )
        assert log.match(
            levelno=logging.DEBUG,
            message="['git', 'clone', 'clone-url', '{}']".format(expected_path)
        )
    else:
        assert log.match(
            levelno=logging.INFO,
            message="cloning repo clone-url (branch {}, ref not specified)".format(branch)
        )

        assert log.match(
            levelno=logging.DEBUG,
            message="['git', 'clone', '--depth', '1', '-b', '{}', 'clone-url', '{}']".format(branch, expected_path)
        )

    if not (path or prefix):
        mock_mkdtemp.assert_called_with(dir=os.getcwd())


def test_log(remote_git_repository, log):
    remote_git_repository._instance = MagicMock(log=lambda x: 'some-log')
    remote_git_repository.gitlog('some-log')
    assert log.records[-1].message == 'logs found:\n---v---v---v---v---v---\nsome-log\n---^---^---^---^---^---'


@pytest.mark.parametrize('options, expected_error_msg', [
    ({'branch': 'b'}, 'No clone url specified, cannot continue'),
    ({'ref': 'r'}, 'No clone url specified, cannot continue'),
    ({'branch': 'b', 'ref': 'r', 'clone_url': 'c'}, 'Both ref and branch specified, misunderstood arguments?'),
    ({'clone_url': 'c'}, 'Neither ref nor branch specified, cannot continue')
])
def test_clone_sanity(remote_git_repository, options, expected_error_msg):
    for option_name, option_value in options.items():
        setattr(remote_git_repository, option_name, option_value)

    with pytest.raises(gluetool.GlueError, match=expected_error_msg):
        remote_git_repository.clone()


def test_git_initialization_invalid_path(remote_git_repository, monkeypatch):
    mock_git = MagicMock(side_effect=git.exc.GitError('foo'))
    monkeypatch.setattr(git, 'Git', mock_git)
    with pytest.raises(gluetool.GlueError, match="Failed to initialize git repository from path 'some-path': foo"):
        remote_git_repository.initialize_from_path('some-path')


def test_is_cloned():
    remote_git_repository = git_module.RemoteGitRepository(logger=Logging.get_logger(), path='some-path')
    assert remote_git_repository.is_cloned


def test_invalid_clone_path(remote_git_repository, monkeypatch):
    remote_git_repository._instance = MagicMock()
    with pytest.raises(gluetool.GlueError, match='Clone path does not match initialized repository, misunderstood arguments?'):
        remote_git_repository.clone(path='fake-path')


def test_repo_already_initialized(remote_git_repository, monkeypatch):
    remote_git_repository._instance = MagicMock()
    assert 'some-path' == remote_git_repository.clone(path='some-path')


def test_clone_shallow_failed(remote_git_repository, monkeypatch, log):
    remote_git_repository.branch = 'some-branch'
    remote_git_repository.clone_url = 'clone-url'
    monkeypatch.setattr(os, 'chmod', MagicMock())

    # git.py, unlike all other modules, is the only one that uses cmd.options to pass variables to Command.run()
    # to be able to assert it, we decided to use this workaround of creating a MockCommand class and asserting the log
    # to make sure it is called with the right parameters.
    class MockCommand():
        def __init__(self, cmd, logger=None):
            self.options = []
            self.executable = []

        def run(self):
            remote_git_repository.logger.info(self.options)
            mock_output = MagicMock(stderr='dumb http transport does not support shallow capabilities')
            raise gluetool.GlueCommandError(cmd='some-cmd', output=mock_output)

    monkeypatch.setattr(gluetool.utils, 'Command', MockCommand)

    with pytest.raises(gluetool.GlueError, match="Condition 'cloning with timeout 2s, tick 1s' failed to pass within given time"):
        remote_git_repository.clone(clone_timeout=2, clone_tick=1)

    assert log.match(levelno=logging.INFO, message="['--depth', '1', '-b', 'some-branch', 'clone-url', 'some-path']")
    assert log.match(levelno=logging.INFO, message="['-b', 'some-branch', 'clone-url', 'some-path']")


def test_clone_invalid_ref(remote_git_repository, monkeypatch):
    remote_git_repository.ref = 'some-ref'
    remote_git_repository.clone_url = 'clone-url'
    monkeypatch.setattr(os, 'chmod', MagicMock())
    monkeypatch.setattr(gluetool.utils, 'wait', MagicMock())

    mock_output = MagicMock(stderr='some cloning error')
    mock_command_run = MagicMock(side_effect=gluetool.GlueCommandError(cmd='some-cmd', output=mock_output))
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    mock_get_clone_options = MagicMock()
    monkeypatch.setattr(remote_git_repository, '_get_clone_options', mock_get_clone_options)

    with pytest.raises(gluetool.GlueError, match="Failed to checkout ref some-ref: some cloning error"):
        remote_git_repository.clone(clone_timeout=1, clone_tick=1)


@pytest.mark.parametrize('path, ref, calls', [
    (
        'some-path', 'some-ref',
        [
            ['git', '-C', 'some-path', 'show-ref', '-s', 'some-ref'],
            ['git', '-C', 'some-path', 'checkout', '-b', 'some-ref-testing-farm-checkout', 'some-ref']
        ]
    ),
    (
        'some-path', 'refs/remotes/origin/merge-requests/1/head',
        [
            [
                'git', '-C', 'some-path', 'config', 'remote.origin.fetch',
                '+refs/merge-requests/*:refs/remotes/origin/merge-requests/*'
            ],
            [
                'git', '-C', 'some-path', 'fetch', 'some-url',
                'refs/remotes/origin/merge-requests/1/head:refs/remotes/origin/merge-requests/1/head'
            ],
            [
                'git', '-C', 'some-path', 'checkout', 'refs/remotes/origin/merge-requests/1/head'
            ],
        ]
    )
])
def test_checkout_ref(remote_git_repository, monkeypatch, path, ref, calls):
    mock_command_instance = MagicMock()
    mock_command_instance.run = lambda: MagicMock(stdout=ref)
    mock_command_class = MagicMock(return_value=mock_command_instance)

    monkeypatch.setattr(gluetool.utils, 'Command', mock_command_class)

    remote_git_repository.clone_url = 'some-url'
    remote_git_repository._checkout_ref(path, ref)

    for call in calls:
        mock_command_class.assert_any_call(call)

    assert len(mock_command_class.mock_calls) == len(calls)


@pytest.mark.parametrize('self_ref, ref, expected', [
    ('foo', 'bar', 'bar'),
    (None, 'bar', 'bar'),
    ('foo', None, 'foo')
])
def test_clone_obeys_ref(self_ref, ref, expected, remote_git_repository, monkeypatch):
    monkeypatch.setattr(remote_git_repository, '_checkout_ref', MagicMock())
    remote_git_repository.ref = self_ref
    remote_git_repository.clone_url = "foo"
    monkeypatch.setattr(gluetool.utils, 'wait', MagicMock())
    monkeypatch.setattr(os, 'chmod', MagicMock())

    remote_git_repository.clone(ref=ref)

    remote_git_repository._checkout_ref.assert_called_with('some-path', expected)


@pytest.mark.parametrize('clone_url, branch, ref, repr, prefix', [
    (
        'some-url', 'some-branch', None,
        '<RemoteGitRepository(clone_url=some-url, branch=some-branch, ref=not specified)>',
        'git-some-branch'
    ),
    (
        'some-url', None, 'some-ref',
        '<RemoteGitRepository(clone_url=some-url, branch=not specified, ref=some-ref)>',
        'git-some-ref'
    ),
    (
        'some-url', None, 'refs/merge-requests/15/head',
        '<RemoteGitRepository(clone_url=some-url, branch=not specified, ref=refs/merge-requests/15/head)>',
        'git-refs-merge-requests-15-head'
    )
])
def test_repr_clonedir_prefix(clone_url, branch, ref, repr, prefix, remote_git_repository):
    remote_git_repository.clone_url = clone_url
    remote_git_repository.branch = branch
    remote_git_repository.ref = ref

    assert str(remote_git_repository) == repr
    assert remote_git_repository.clonedir_prefix == prefix
