# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import json
import tempfile

import pytest

import gluetool
import gluetool_modules_framework.helpers.ansible
import gluetool_modules_framework.libs.testing_environment
import gluetool_modules_framework.libs.guest as guest_module

from gluetool_modules_framework.helpers.ansible import DEFAULT_ANSIBLE_PYTHON_INTERPRETERS

import mock
from mock import MagicMock

from . import create_module, check_loadable


ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets', 'ansible')


@pytest.fixture(name='module')
def fixture_module():
    module = create_module(gluetool_modules_framework.helpers.ansible.Ansible)[1]
    module._config['ansible-playbook-options'] = []
    module._config['ansible-playbook-filepath'] = '/usr/bin/ansible-playbook'
    module._config['use-pipelining'] = True
    module._config['ansible-playbook-environment-variables'] = 'ENV1=VAL1,ENV2=VAL2'
    return module


@pytest.fixture(name='local_guest')
def fixture_local_guest(module):
    guest = guest_module.NetworkedGuest(
        module, '127.0.0.1',
        key='dummy_key', options=['name=value'], username="gluetool"
    )
    guest.environment = gluetool_modules_framework.libs.testing_environment.TestingEnvironment(
        arch='x86_64',
        compose='dummy-compose'
    )

    return guest


@pytest.fixture(name='assert_output')
def fixture_assert_output():
    # https://stackoverflow.com/questions/22627659/run-code-before-and-after-each-test-in-py-test
    yield

    assert os.path.exists(gluetool_modules_framework.helpers.ansible.ANSIBLE_OUTPUT)
    os.unlink(gluetool_modules_framework.helpers.ansible.ANSIBLE_OUTPUT)


def test_sanity(module):
    pass


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/helpers/ansible.py', 'Ansible')


def test_shared(module):
    assert module.glue.has_shared('run_playbook')


def test_run_playbook_json(module, local_guest, monkeypatch, assert_output):
    json_output = {'task': 'ok'}
    mock_output = MagicMock(exit_code=0, stdout=json.dumps(json_output), stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    output = module.run_playbook('dummy playbook file', local_guest, json_output=True)

    assert output.execution_output is mock_output
    assert output.json_output == json_output

    mock_command_init.assert_called_once_with([
        '/usr/bin/ansible-playbook',
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        os.path.abspath('dummy playbook file')
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'json', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd=None, env=env_variables)


def test_run_playbook_plaintext(module, local_guest, monkeypatch, assert_output):
    mock_output = MagicMock(exit_code=0, stdout='', stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    output = module.run_playbook('dummy playbook file', local_guest)

    assert output.execution_output is mock_output
    assert output.json_output is None

    mock_command_init.assert_called_once_with([
        '/usr/bin/ansible-playbook',
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        '-v',
        os.path.abspath('dummy playbook file'),
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'debug', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd=None, env=env_variables)


def test_run_playbooks(module, local_guest, monkeypatch, assert_output):
    mock_output = MagicMock(exit_code=0, stdout='', stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    output = module.run_playbook(
        ['playbook1', 'playbook2'],
        local_guest,
        json_output=False,
        extra_options=['-t', 'classic']
    )

    assert output.execution_output is mock_output
    assert output.json_output is None

    mock_command_init.assert_called_once_with([
        '/usr/bin/ansible-playbook',
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        '-t', 'classic',
        '-v',
        os.path.abspath('playbook1'),
        os.path.abspath('playbook2')
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'debug', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd=None, env=env_variables)


@pytest.mark.parametrize('config, expected', [
    (  # Set ansible-playbook filepath using direct option
        {
            'ansible-playbook-filepath': '/foo/bar/ansible-playbook'
        },
        '/foo/bar/ansible-playbook'),
    (  # Set ansible-playbook filepath using mapping file
        {
            'ansible-playbook-filepath': None,
            'compose-to-ansible-playbook-map-filepath': os.path.join(ASSETS_DIR, 'compose-to-filepath.yaml')
        },
        '/mapped/path/ansible-playbook'
    ),
    (  # Use both options, `ansible-playbook-filepath` has priority
        {
            'ansible-playbook-filepath': '/foo/bar/ansible-playbook',
            'compose-to-ansible-playbook-map-filepath': os.path.join(ASSETS_DIR, 'compose-to-filepath.yaml')
        },
        '/foo/bar/ansible-playbook'
    ),
])
def test_change_ansible_playbook_filepath_option(module, local_guest, monkeypatch, assert_output, config, expected):
    for option_name, option_value in config.items():
        module._config[option_name] = option_value

    mock_output = MagicMock(exit_code=0, stdout='', stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    output = module.run_playbook(['playbook1', 'playbook2'], local_guest, json_output=False)

    assert output.execution_output is mock_output
    assert output.json_output is None

    mock_command_init.assert_called_once_with([
        expected,
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        '-v',
        os.path.abspath('playbook1'),
        os.path.abspath('playbook2')
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'debug', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd=None, env=env_variables)


def test_change_ansible_playbook_filepath_argument(module, local_guest, monkeypatch, assert_output):

    mock_output = MagicMock(exit_code=0, stdout='', stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    output = module.run_playbook(
        ['playbook1', 'playbook2'],
        local_guest, json_output=False,
        ansible_playbook_filepath='/foo/bar/ansible-playbook'
    )

    assert output.execution_output is mock_output
    assert output.json_output is None

    mock_command_init.assert_called_once_with([
        '/foo/bar/ansible-playbook',
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        '-v',
        os.path.abspath('playbook1'),
        os.path.abspath('playbook2')
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'debug', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd=None, env=env_variables)


def test_error(log, module, local_guest, monkeypatch, assert_output):
    # simulate output of failed ansible-playbook run, giving user JSON blob with an error message
    mock_error = gluetool.GlueCommandError([], output=MagicMock(stdout='{"msg": "dummy error message"}', stderr=''))
    mock_command_run = MagicMock(side_effect=mock_error)

    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    with pytest.raises(gluetool.GlueError, match='Failure during Ansible playbook execution'):
        module.run_playbook('dummy playbook file', local_guest)


def test_error_exit_code(log, module, local_guest, monkeypatch, assert_output):
    mock_output = MagicMock(exit_code=1, stdout='{"msg": "dummy error message"}', stderr='')
    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    with pytest.raises(gluetool.GlueError, match='Failure during Ansible playbook execution'):
        module.run_playbook('dummy playbook file', local_guest)


def test_extra_vars(module, local_guest, monkeypatch, assert_output):
    mock_output = MagicMock(exit_code=0, stdout=json.dumps({'task': 'ok'}), stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    module.run_playbook('dummy playbook file', local_guest, variables={
        'FOO': 'bar'
    }, cwd='foo')

    mock_command_init.assert_called_once_with([
        '/usr/bin/ansible-playbook',
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        '--extra-vars', 'FOO="bar"',
        '-v',
        os.path.abspath('dummy playbook file')
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'debug', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd='foo', env=env_variables)


def test_dryrun(module, local_guest, monkeypatch, assert_output):
    mock_output = MagicMock(exit_code=0, stdout=json.dumps({'task': 'ok'}), stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    monkeypatch.setattr(module.glue, '_dryrun_level', gluetool.glue.DryRunLevels.DRY)

    module.run_playbook('dummy playbook path', local_guest)

    mock_command_init.assert_called_once_with([
        '/usr/bin/ansible-playbook',
        '-i', '127.0.0.1,',
        '--private-key', local_guest.key,
        '--user', local_guest.username,
        '-C',
        '-v',
        os.path.abspath('dummy playbook path')
    ], logger=local_guest.logger)

    env_variables = os.environ.copy()
    env_variables.update({'ANSIBLE_STDOUT_CALLBACK': 'debug', 'ANSIBLE_PIPELINING': 'True'})
    env_variables.update({'ENV1': 'VAL1', 'ENV2': 'VAL2'})

    mock_command_run.assert_called_once_with(cwd=None, env=env_variables)


def test_additonal_options(module, local_guest, monkeypatch, assert_output):
    mock_output = MagicMock(exit_code=0, stdout=json.dumps({'task': 'ok'}), stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)
    module._config['ansible-playbook-options'] = ['-vvv', '-d']

    module.run_playbook('dummy playbook file', local_guest, variables={
        'FOO': 'bar'
    })

    mock_command_init.assert_called_once_with([
        '/usr/bin/ansible-playbook', '-i', '127.0.0.1,', '--private-key', local_guest.key,
        '--user', local_guest.username,
        '--extra-vars', 'FOO="bar"',
        '-vvv',
        '-d',
        '-v',
        os.path.abspath('dummy playbook file')
    ], logger=local_guest.logger)


def test_detect_ansible_interpreter(module, local_guest, monkeypatch):
    mock_output = MagicMock(exit_code=0, stdout='/usr/bin/python3', stderr='')

    mock_command_init = MagicMock(return_value=None)
    mock_command_run = MagicMock(return_value=mock_output)

    monkeypatch.setattr(gluetool.utils.Command, '__init__', mock_command_init)
    monkeypatch.setattr(gluetool.utils.Command, 'run', mock_command_run)

    available_interpreters = module.detect_ansible_interpreter(local_guest)

    assert available_interpreters == ['/usr/bin/python3']

    mock_command_init.assert_called_once_with([
        'ansible',
        '--inventory', '{},'.format(local_guest.hostname),
        '--private-key', local_guest.key,
        '--module-name', 'raw',
        '--args', 'command -v ' + ' '.join(DEFAULT_ANSIBLE_PYTHON_INTERPRETERS),
        '--ssh-common-args=' + ' '.join(['-o ' + option for option in local_guest.options]),
        '--user', local_guest.username,
        local_guest.hostname,
    ], logger=local_guest.logger)


def test_render_extra_variables_templates(module, monkeypatch):
    templates = []

    # generate templates
    for _ in range(3):
        with tempfile.NamedTemporaryFile(delete=False) as template:
            template.write(
                b'---\noption1: {{ value }}\noption2: {{ value }}'
            )
            templates.append(template.name)

    module._config['extra-variables-template-file'] = ','.join(templates)

    # render template files
    rendered_templates = module.render_extra_variables_templates(
        module.logger,
        {
            'value': 'testvalue'
        }
    )

    for template in rendered_templates:
        with open(template, 'rb') as template:
            assert template.read() == b'---\noption1: testvalue\noption2: testvalue'

    # cleanup temporary files
    for template in templates + rendered_templates:
        os.unlink(template)
