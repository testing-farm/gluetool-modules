# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import pytest
import koji
import functools
import six
import requests.exceptions

import gluetool
import gluetool_modules_framework.helpers.rules_engine
import gluetool_modules_framework.libs.artifacts
import gluetool_modules_framework.infrastructure.koji_fedora


from mock import MagicMock
from . import create_module, patch_shared, testing_asset


class MockClientSession(object):
    """
    Mocked Koji session. It is given a source file which provides all necessary responses. The session
    reads the data, mocks its methods and replies accordingly to queries.
    """

    def __init__(self, source_file):
        # do not use gluetool.utils.load_yaml here, as log_dict is not happy about integer keys
        # we use in the test data, because this is something synthetic, rather workaround it here
        with open(source_file, 'r') as file:
            data = gluetool.utils.YAML().load(file)

        assert data, 'Empty mock data provided in {}'.format(source_file)

        def getter(name, *args, **kwargs):
            assert name in data, "Attempt to use API endpoint '{}' which is not mocked".format(name)

            if name == 'listBuilds' and not args and 'taskID' in kwargs:
                args = (kwargs['taskID'],)

            if args:
                assert args[0] in data[name], "Attempt to use API endpoint '{}({})' which is not mocked".format(
                    name, args[0])

                return data[name][args[0]]

            return data[name]

        for method, response in six.iteritems(data):
            setattr(self, method, functools.partial(getter, method))


@pytest.fixture(name='koji_session')
def fixture_koji_session(request, monkeypatch):
    # This is a bit complicated. We want parametrize this fixture, which is what indirect=True
    # does, but that somehow expecteds that all params are given to this fixture, while we want
    # thise give it just the task ID, and other params, e.g. NVR, are for the test itself.
    # To overcome that, request.params can be multiple packed params, this fixture will use
    # just the first one (task ID), return all of them, and test needs to unpack them as necessary.

    task_id = request.param[0] if isinstance(request.param, tuple) else request.param

    session = MockClientSession(testing_asset(os.path.join('koji', '{}.yml'.format(task_id))))

    monkeypatch.setattr(koji, 'ClientSession', MagicMock(return_value=session))

    return request.param


@pytest.fixture(name='rules_engine')
def fixture_rules_engine():
    return create_module(gluetool_modules_framework.helpers.rules_engine.RulesEngine)[1]


@pytest.fixture(name='koji_module')
def fixture_koji_module(monkeypatch, rules_engine):
    ci, mod = create_module(gluetool_modules_framework.infrastructure.koji_fedora.Koji)

    # make sure task has required share function
    assert ci.has_shared('tasks')
    assert ci.has_shared('primary_task')

    mod._config = {
        'url': 'https://koji.fedoraproject.org/kojihub',
        'pkgs-url': 'https://kojipkgs.fedoraproject.org',
        'web-url': 'https://koji.fedoraproject.org/koji',
        'baseline-tag-map': testing_asset('koji', 'baseline-tag-map.yaml'),
        'api-version-retry-tick': 1,
        'api-version-retry-timeout': 2
    }

    patch_shared(monkeypatch, mod, {}, callables={
        'evaluate_instructions': rules_engine.evaluate_instructions,
        'evaluate_rules': rules_engine.evaluate_rules
    })

    # Every call to Koji's execute() tries to connect to real-life Koji server. Now that's not good, in unit tests.
    # Patching _call_api to hijack this call, but letting others pass through because tests usually patch Koji
    # session to provide mock data.
    orig_call_api = gluetool_modules_framework.infrastructure.koji_fedora._call_api

    def _patch_call_api(*args, **kwargs):
        if args[2] == 'getAPIVersion':
            return 'dummy-koji-version'

        return orig_call_api(*args, **kwargs)

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.koji_fedora, '_call_api', _patch_call_api)

    #  make sure the module is loaded without a task specified
    mod.execute()

    return mod


@pytest.fixture(name='brew_module')
def fixture_brew_module(monkeypatch, rules_engine):
    ci, mod = create_module(gluetool_modules_framework.infrastructure.koji_fedora.Brew)

    # make sure task has required share function
    assert ci.has_shared('tasks')
    assert ci.has_shared('primary_task')

    mod._config = {
        'url': 'https://kojipkgs.fedoraproject.org/brewhub',
        'pkgs-url': 'http://kojipkgs.fedoraproject.org/brewroot',
        'web-url': 'https://kojipkgs.fedoraproject.org.com/brew',
        'automation-user-ids': '2863',
        'dist-git-commit-urls': 'http://kojipkgs.fedoraproject.org/cgit/rpms/{component}/commit/?id={commit},http://kojipkgs.fedoraproject.org/cgit/rpms/{component}.git/commit/?id={commit}',
        'docker-image-url-template': "{{ MODULE.option('pkgs-url') }}/packages/{{ TASK.component }}/{{ TASK.version }}/{{ TASK.release }}/images/{{ ARCHIVE['filename'] }}",
        'baseline-tag-map': testing_asset('koji', 'baseline-tag-map.yaml'),
        'api-version-retry-tick': 1,
        'api-version-retry-timeout': 2
    }

    patch_shared(monkeypatch, mod, {}, callables={
        'evaluate_instructions': rules_engine.evaluate_instructions,
        'evaluate_rules': rules_engine.evaluate_rules
    })

    # Every call to Koji's execute() tries to connect to real-life Koji server. Now that's not good, in unit tests.
    # Patching _call_api to hijack this call, but letting others pass through because tests usually patch Koji
    # session to provide mock data.
    orig_call_api = gluetool_modules_framework.infrastructure.koji_fedora._call_api

    def _patch_call_api(*args, **kwargs):
        if args[2] == 'getAPIVersion':
            return 'dummy-koji-version'

        return orig_call_api(*args, **kwargs)

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.koji_fedora, '_call_api', _patch_call_api)

    #  make sure the module is loaded without a task specified
    mod.execute()

    return mod


def assert_task_attributes(module, task_id):
    """
    Assert helper. Given the task ID, it loads expected values of a task from the YAML file,
    and compares them to actual values of module's primary task.
    """

    primary_task = module.primary_task()

    expected_attributes = gluetool.utils.load_yaml(testing_asset('koji', 'task-{}.yml'.format(task_id)))

    for name, expected in six.iteritems(expected_attributes):
        actual = getattr(primary_task, name)

        # correctly interpret 'None' value
        if expected == 'None':
            expected = None

        assert actual == expected, "Field '{}' mismatch: {} expected, {} found".format(name, expected, actual)


@pytest.mark.parametrize('koji_session', [
    15869828,
    20166983,
    16311217
], indirect=True)
def test_task_by_id(koji_session, koji_module):
    """
    Tasks are specified directly by their IDs.
    """

    koji_module.tasks(task_ids=[koji_session])

    assert_task_attributes(koji_module, koji_session)


@pytest.mark.parametrize('koji_session', [
    48742482
], indirect=True)
def test_brew_task_by_id(koji_session, brew_module):
    """
    Tasks are specified directly by their IDs.
    """

    brew_module.tasks(task_ids=[koji_session])

    assert_task_attributes(brew_module, koji_session)


@pytest.mark.parametrize('koji_session', [
    (15869828, True),
    (20166983, False),
    (16311217, True)
], indirect=True)
def test_task_by_task_id_option(koji_session, koji_module):
    """
    Tasks are specified via module's ``--task-id`` option.
    """

    task_id, has_artifacts = koji_session

    koji_module._config['task-id'] = [task_id]

    koji_module.execute()

    if has_artifacts:
        gluetool_modules_framework.libs.artifacts.has_artifacts(*koji_module.tasks())

    else:
        with pytest.raises(gluetool_modules_framework.libs.artifacts.NoArtifactsError):
            gluetool_modules_framework.libs.artifacts.has_artifacts(*koji_module.tasks())

    assert_task_attributes(koji_module, task_id)


@pytest.mark.parametrize('koji_session', [
    (15869828, 'bash-4.3.43-4.fc25')
], indirect=True)
def test_task_by_nvr_option(koji_session, koji_module):
    """
    Tasks are specified via module's ``--nvr`` option.
    """

    task_id, nvr = koji_session

    koji_module._config['nvr'] = [nvr]

    koji_module.execute()

    assert_task_attributes(koji_module, task_id)


@pytest.mark.parametrize('koji_session', [
    (15869828, 805705)
], indirect=True)
def test_task_by_build_id_option(koji_session, koji_module):
    """
    Tasks are specified via module's ``--build-id`` option.
    """

    task_id, build_id = koji_session

    koji_module._config['build-id'] = [build_id]

    koji_module.execute()

    assert_task_attributes(koji_module, task_id)


@pytest.mark.parametrize('koji_session', [
    (15869828, 'bash', 'f25')
], indirect=True)
def test_task_by_name_and_tag_options(koji_session, koji_module):
    """
    Tasks are specified via module's ``--name`` and ``--tag`` options.
    """

    task_id, name, tag = koji_session

    koji_module._config.update({
        'name': name,
        'tag': tag
    })

    koji_module.execute()

    assert_task_attributes(koji_module, task_id)


def test_no_koji_task(koji_module):
    """
    Module haven't been told to represent any tasks yet, however someone already asks for them.
    """

    assert koji_module.tasks() == []


def test_invalid_task_id_type(koji_module):
    """
    Invalid task ID passed to the module.
    """

    with pytest.raises(ValueError):
        koji_module.tasks(task_ids=['invalid id'])


@pytest.mark.parametrize('koji_session', [
    20171466
], indirect=True)
def test_not_valid_build_tasks(koji_session, koji_module):
    """
    Tasks IDs represent tasks that are not valid build tasks.
    """

    koji_module._config['valid-methods'] = ['build']

    with pytest.raises(gluetool.GlueError, match=r'Task is not a build task'):
        koji_module.tasks(task_ids=[koji_session])


def test_missing_name_option(koji_module):
    koji_module._config['tag'] = 'f25'

    with pytest.raises(gluetool.GlueError, match=r"You need to specify package name with '--name' option"):
        koji_module.sanity()


def test_missing_tag_option(koji_module):
    koji_module._config['name'] = 'bash'

    with pytest.raises(gluetool.GlueError, match=r"You need to specify 'tag' with package name"):
        koji_module.sanity()


@pytest.mark.parametrize('koji_session', [
    705705
], indirect=True)
def test_invalid_build(koji_session, koji_module, log):
    koji_module._config['build-id'] = [koji_session]

    koji_module.execute()

    assert log.match(
        levelno=logging.WARN,
        message='Looking for build 705705, remote server returned None - skipping this ID'
    )
    assert koji_module._tasks == []


@pytest.mark.parametrize('koji_session', [
    10166983
], indirect=True)
def test_request_missing(koji_session, koji_module):
    with pytest.raises(gluetool.GlueError, match=r'Task 10166983 has no request field in task info'):
        koji_module.tasks(task_ids=[koji_session])


@pytest.mark.parametrize('koji_session', [
    10166985
], indirect=True)
def test_request_length_invalid(koji_session, koji_module):
    with pytest.raises(gluetool.GlueError, match=r'Task 10166985 has unexpected number of items in request field'):
        koji_module.tasks(task_ids=[10166985])


@pytest.mark.parametrize('koji_session', [
    15869828,
], indirect=True)
def test_invalid_tag_latest(koji_session, koji_module, log):
    """
    Test if latestTagged api call traceback is correctly handled
    """

    koji_module.tasks(task_ids=[koji_session])
    task = koji_module.primary_task()

    # we need to first cache candidate, target and destination_tag as we will be mocking _call_api
    task.component
    task.destination_tag
    task.target

    # make _call_api traceback as latest_released calls it to simulate the behaviour
    task._call_api = MagicMock(side_effect=koji.GenericError('koji error'))

    assert task.latest_released() == None
    assert log.match(
        levelno=logging.WARN,
        message="ignoring error while listing latest builds tagged to 'f25-updates-candidate': koji error"
    )
    assert log.match(
        levelno=logging.WARN,
        message="ignoring error while listing latest builds tagged to 'f25-candidate': koji error"
    )


@pytest.mark.parametrize('koji_session', [
    (15869828, 'previous-released-build', 'bash-4.2.43-4.fc24'),
    (15869828, 'previous-build', 'bash-4.3.43-3.fc25'),
    (15869828, 'specific-build', 'bash-1.1.1-1.fc1'),

], indirect=True)
def test_baseline(koji_session, koji_module, log):
    """
    Test if baseline builds are correctly resolved
    """

    task_id, method, nvr = koji_session

    koji_module._config['baseline-method'] = method
    koji_module._config['task-id'] = [task_id]
    koji_module._config['baseline-nvr'] = nvr

    koji_module.execute()

    # if the previous release build is the same, there is no baseline task
    if nvr is None:
        assert log.match(levelno=logging.DEBUG, message="Baseline task is the same, ignoring")
        assert log.match(levelno=logging.WARN, message="Baseline build was not found")
        koji_module._tasks[0].baseline_task == None
        return

    assert koji_module._tasks[0].baseline_task.nvr == nvr
    assert koji_module._tasks[0].baseline == nvr


@pytest.mark.parametrize('koji_session', [
    15869828,
], indirect=True)
def test_server_offline(koji_session, koji_module, monkeypatch):
    """
    Tests if api version call is retried on server offline
    """

    api_mock = MagicMock(side_effect=koji.ServerOffline)
    monkeypatch.setattr(koji_module, '_call_api', api_mock)

    with pytest.raises(gluetool.GlueError, match=r"Condition 'getting api version' failed to pass within given time"):
        koji_module.execute()

    assert len(api_mock.mock_calls) > 1


@pytest.mark.parametrize('koji_session', [
    15869828,
], indirect=True)
def test_server_issue(koji_session, koji_module, monkeypatch):
    """
    Tests if api version call is retried on server error
    """

    api_mock = MagicMock(side_effect=requests.exceptions.ConnectionError)
    monkeypatch.setattr(koji_module, '_call_api', api_mock)

    with pytest.raises(gluetool.GlueError, match=r"Condition 'getting api version' failed to pass within given time"):
        koji_module.execute()

    assert len(api_mock.mock_calls) > 1


@pytest.mark.parametrize('koji_session', [
    (15869828, 'koji-build'),
    (15869828, 'brew-build'),
    (15869828, 'redhat-container-image')
], ids=lambda x: x[0], indirect=True)
def test_artifact_namespace(koji_session, koji_module, brew_module, log):
    """
    Test if artifact namespace correctly set and can be forced.
    """

    task_id, artifact = koji_session

    module_map = {
        'koji-build': koji_module,
        'brew-build': brew_module,
        'redhat-container-image': brew_module
    }

    module = module_map[artifact]

    module._config['task-id'] = [task_id]

    # custom artifact namespace
    if artifact == 'redhat-container-image':
        module._config['artifact-namespace'] = artifact

    module.execute()
    assert module.primary_task().ARTIFACT_NAMESPACE == artifact

    # custom artifact namespace
    if artifact == 'redhat-container-image':
        assert log.match(
            levelno=logging.WARN,
            message="Forcing ARTIFACT_NAMESPACE to '{}'".format(artifact)
        )
