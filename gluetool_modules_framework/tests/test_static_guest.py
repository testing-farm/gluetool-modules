# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

# SPDX-License-Identifier: Apache-2.0

import pytest
from mock import MagicMock

import gluetool
from gluetool.utils import IncompatibleOptionsError
import gluetool_modules_framework.infrastructure.static_guest
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment
from . import create_module, check_loadable, patch_shared


class MockStaticGuest(MagicMock):
    @staticmethod
    def execute(cmd, ssh_options=None, connection_timeout=None, **kwargs):
        value = MagicMock()
        if cmd == 'arch':
            value.return_code.stdout = 'x86_64'
        return value


@pytest.fixture(name='module')
def fixture_module():
    return create_module(gluetool_modules_framework.infrastructure.static_guest.CIStaticGuest)[1]


@pytest.fixture(name='module_with_remote_guest')
def fixture_module_with_remote_guest(monkeypatch):
    module = create_module(gluetool_modules_framework.infrastructure.static_guest.CIStaticGuest)[1]

    module._config['ssh-key'] = 'very-real-ssh-key'

    mock_execute = MagicMock()
    mock_execute.return_value.stdout = 'x86_64'

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticGuest, 'wait_alive', MagicMock())
    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticGuest, 'execute', mock_execute)

    guest = module.guest_remote('user@hostname:22')

    module._guests.append(guest)

    return module


@pytest.fixture(name='module_with_localhost_guest')
def fixture_module_with_localhost_guest(monkeypatch):
    module = create_module(gluetool_modules_framework.infrastructure.static_guest.CIStaticGuest)[1]

    mock_execute = MagicMock()
    mock_execute.return_value.stdout = 'x86_64'

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticLocalhostGuest,
                        'execute', mock_execute)

    guest = module.guest_localhost('localhost')

    module._guests.append(guest)

    return module


def test_loadable(module):
    check_loadable(module.glue, 'gluetool_modules_framework/infrastructure/static_guest.py', 'CIStaticGuest')


def test_sanity_no_guest(module):
    sanity_return = module.sanity()
    assert sanity_return == None


def test_sanity_remote_guest_no_ssh(module):
    module._config['guest'] = ['user@hostname:22']

    with pytest.raises(IncompatibleOptionsError) as excinfo:
        module.sanity()
    assert str(excinfo.value) == "Option 'ssh-key' is required"


def test_sanity_remote_guest(module):
    module._config['guest'] = ['user@hostname:22']
    module._config['ssh-key'] = 'very-real-ssh-key'

    sanity_return = module.sanity()
    assert sanity_return == None


def test_execute_remote_not_valid_hostname(module):
    module._config['guest'] = ['user@host@:name:port']

    with pytest.raises(gluetool.GlueError) as excinfo:
        module.execute()

    assert "is not a valid hostname" in str(excinfo.value)


def test_execute_remote_connection_error(module):
    module._config['guest'] = ['user@hostname:22']

    with pytest.raises(gluetool.GlueError) as excinfo:
        module.execute()

    assert "Error connecting to guest" in str(excinfo.value)


def test_execute_remote_no_arch(monkeypatch, module):
    module._config['guest'] = ['user@hostname:22']
    module._config['ssh-key'] = 'very-real-ssh-key'

    execute_results = MagicMock()
    execute_results.return_value.stdout = None

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticGuest, 'wait_alive', MagicMock())
    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticGuest, 'execute', execute_results)

    with pytest.raises(gluetool.GlueError) as excinfo:
        module.execute()
    assert 'Error retrieving guest architecture' in str(excinfo.value)


def test_execute_remote(monkeypatch, module):
    module._config['guest'] = ['user@hostname:22']
    module._config['ssh-key'] = 'very-real-ssh-key'
    module._config['guest-setup'] = True

    patch_shared(monkeypatch, module, {
        'setup_guest': MagicMock()
    })

    mock_execute = MagicMock()
    mock_execute.return_value.stdout = 'x86_64'

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticGuest, 'wait_alive', MagicMock())
    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticGuest, 'execute', mock_execute)

    module.execute()

    assert len(module._guests) == 1
    assert module._guests[0].hostname == 'hostname'
    assert module._guests[0].name == 'hostname'
    assert module._guests[0].username == 'user'
    assert module._guests[0].port == 22
    assert module._guests[0].key == 'very-real-ssh-key'
    assert module._guests[0].environment.arch == 'x86_64'
    assert module._guests[0]._is_allowed_degraded('service') == True

    with pytest.raises(NotImplementedError):
        module._guests[0].destroy()


def test_execute_local_no_arch(monkeypatch, module):
    module._config['guest'] = ['localhost']

    mock_execute = MagicMock()
    mock_execute.return_value.stdout = None

    monkeypatch.setattr(gluetool_modules_framework.infrastructure.static_guest.StaticLocalhostGuest,
                        'execute', mock_execute)

    with pytest.raises(gluetool.GlueError) as excinfo:
        module.execute()
    assert 'Error retrieving guest architecture' in str(excinfo.value)


def test_execute_local(monkeypatch, module):
    module._config['guest'] = ['localhost']
    module._config['guest-setup'] = True

    patch_shared(monkeypatch, module, {
        'setup_guest': MagicMock()
    })

    execute_results = MagicMock()
    execute_results.stdout = 'x86_64'

    module.execute()

    assert len(module._guests) == 1
    assert module._guests[0].hostname == 'localhost'
    assert module._guests[0].environment.arch == 'x86_64'
    assert module._guests[0]._is_allowed_degraded('service') == True

    with pytest.raises(NotImplementedError):
        module._guests[0].destroy()


def test_execute_local_no_setup_guest(monkeypatch, module):
    module._config['guest'] = ['localhost']
    module._config['guest-setup'] = True

    with pytest.raises(gluetool.GlueError) as excinfo:
        module.execute()
    assert "Module 'guest-setup' is required to actually set the guests up." in str(excinfo.value)


def test_provisioner_capabilities_remote(module_with_remote_guest):
    provisioner_capabilities = module_with_remote_guest.provisioner_capabilities()
    assert provisioner_capabilities
    assert provisioner_capabilities.available_arches == ['x86_64']


def test_provisioner_capabilities_localhost(module_with_localhost_guest):
    provisioner_capabilities = module_with_localhost_guest.provisioner_capabilities()
    assert provisioner_capabilities
    assert provisioner_capabilities.available_arches == ['x86_64']


def test_provision_no_guests(module):
    with pytest.raises(gluetool.GlueError) as excinfo:
        module.provision(TestingEnvironment(arch='x86_64'))

    assert "Did not find 1 guest(s) with architecture 'x86_64'." in str(excinfo.value)


def test_provision_remote(module_with_remote_guest):
    guests = module_with_remote_guest.provision(TestingEnvironment(arch='x86_64'))
    assert module_with_remote_guest._guests == guests

    with pytest.raises(gluetool.GlueError) as excinfo:
        module_with_remote_guest.provision(TestingEnvironment(arch='x86_64'), count=2)

    assert "Did not find 2 guest(s) with architecture 'x86_64'." in str(excinfo.value)


def test_provision_localhost(module_with_localhost_guest):
    guests = module_with_localhost_guest.provision(TestingEnvironment(arch='x86_64'))
    assert module_with_localhost_guest._guests == guests

    with pytest.raises(gluetool.GlueError) as excinfo:
        module_with_localhost_guest.provision(TestingEnvironment(arch='x86_64'), count=2)

    assert "Did not find 2 guest(s) with architecture 'x86_64'." in str(excinfo.value)
