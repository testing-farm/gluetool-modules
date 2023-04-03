# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from gluetool import GlueError
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment


def test_eq():
    assert TestingEnvironment(arch='foo') == TestingEnvironment(arch='foo')
    assert TestingEnvironment(arch='foo') != TestingEnvironment(arch='bar')
    assert TestingEnvironment(arch='foo') != TestingEnvironment(arch='foo', compose='bar')
    assert TestingEnvironment(arch='foo') != 123


def test_serialize():
    env = TestingEnvironment(arch='foo', compose='bar', secrets={'hello': 'world'})

    # Calling str(TestingEnvironment(...)) directly results in a call to TestingEnvironment.__str__()
    assert str(env) == 'arch=foo,compose=bar,secrets=******,snapshots=False'  # noqa

    # Nesting the object into a list and calling str() results in a call to TestingEnvironment.__repr__()
    assert str([env]) == '[<TestingEnvironment(arch=foo,compose=bar,secrets=******,snapshots=False)>]'  # noqa

    assert env.serialize_to_string(hide_secrets=False, show_none_fields=True) == "arch=foo,artifacts=None,compose=bar,hardware=None,kickstart=None,secrets={'hello': 'world'},settings=None,snapshots=False,tmt=None,variables=None"  # noqa
    assert env.serialize_to_json(hide_secrets=False, show_none_fields=True) == {'arch': 'foo', 'artifacts': None, 'compose': 'bar', 'hardware': None, 'kickstart': None, 'secrets': {'hello': 'world'}, 'settings': None, 'snapshots': False, 'tmt': None, 'variables': None}  # noqa


def test_unserialize():
    env = TestingEnvironment(arch='foo', compose='bar', snapshots=False)
    assert TestingEnvironment.unserialize_from_string('arch=foo,compose=bar,snapshots=False') == env

    try:
        TestingEnvironment.unserialize_from_string('compose=bar,snapshots=False') == env
    except TypeError as exc:
        assert str(exc) == "__init__() missing 1 required positional argument: 'arch'"

    env = TestingEnvironment.unserialize_from_string(
        "arch=foo,compose=bar,kickstart={'pre-install':'baz'},snapshots=False,secrets={'hello':'world'}")
    assert env.arch == 'foo'
    assert env.compose == 'bar'
    assert env.snapshots == False
    assert env.secrets == {'hello': 'world'}

    env = TestingEnvironment.unserialize_from_json({'arch': 'foo', 'compose': 'bar', 'kickstart': {'pre-install': 'baz'}, 'snapshots': False, 'secrets': {'hello':'world'}})  # noqa
    assert env.arch == 'foo'
    assert env.compose == 'bar'
    assert env.kickstart == {'pre-install': 'baz'}
    assert env.snapshots == False
    assert env.secrets == {'hello': 'world'}


def test_clone():
    env1 = TestingEnvironment(arch='foo', compose='bar', secrets={'hello': 'world'})
    env2 = env1
    assert env1 == env2
    env1.compose = 'baz'
    assert env1 == env2
    env2 = env1.clone()
    assert env1 == env2
    env2.compose = 'bar'
    env1 != env2
