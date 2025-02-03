# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest

from gluetool import GlueError
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment, dict_nested_value


def test_eq():
    assert TestingEnvironment(arch='foo') == TestingEnvironment(arch='foo')
    assert TestingEnvironment(arch='foo') != TestingEnvironment(arch='bar')
    assert TestingEnvironment(arch='foo') != TestingEnvironment(arch='foo', compose='bar')
    assert TestingEnvironment(arch='foo') != 123


def test_serialize():
    env = TestingEnvironment(
        arch='foo',
        compose='bar',
        secrets={'hello': 'world'},
        tmt={'environment': {'foo': 'foo-value', 'bar': 'bar-value'}}
    )

    # Calling str(TestingEnvironment(...)) directly results in a call to TestingEnvironment.__str__()
    assert str(env) == "arch=foo,compose=bar,secrets=hidden,snapshots=False,tmt={'environment': {'foo': 'hidden', 'bar': 'hidden'}}"  # noqa

    # Nesting the object into a list and calling str() results in a call to TestingEnvironment.__repr__()
    assert str([env]) == "[<TestingEnvironment(arch=foo,compose=bar,secrets=hidden,snapshots=False,tmt={'environment': {'foo': 'hidden', 'bar': 'hidden'}})>]"  # noqa

    expected_serialized_string = (
           "arch=foo,artifacts=None,compose=bar,hardware=None,"
            "kickstart=None,secrets={'hello': 'world'},settings=None,snapshots=False,"
            "tmt={'environment': {'foo': 'foo-value', 'bar': 'bar-value'}},variables=None"
    )
    expected_serialized_json = {
            'arch': 'foo', 'artifacts': None, 'compose': 'bar', 'hardware': None, 'kickstart': None,
            'secrets': {'hello': 'world'}, 'settings': None, 'snapshots': False,
            'tmt': {'environment': {'bar': 'bar-value', 'foo': 'foo-value'}}, 'variables': None
    }

    assert env.serialize_to_string(hide_secrets=False, show_none_fields=True) == expected_serialized_string
    assert env.serialize_to_json(hide_secrets=False, show_none_fields=True) == expected_serialized_json


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
    assert env1 != env2


@pytest.mark.parametrize('test_name, dictionary, keys, expected', [
    (
        'returning a list',
        {
            'a': [
                'b',
                'c'
            ]
        },
        ('a'),
        [
            'b',
            'c'
        ]
    ),
    (
        'unknown key',
        {
            'a': [
                'b',
                'c'
            ]
        },
        ('b'),
        None
    ),
    (
        'returning a dictionary',
        {
            'a': {
                'b': {
                    'c': 'd'
                }
            }
        },
        ('a'),
        {
            'b': {
                'c': 'd'
            }
        }
    ),
    (
        'returning a nested dictionary',
        {
            'a': {
                'b': {
                    'c': 'd'
                }
            }
        },
        ('a', 'b'),
        {
            'c': 'd'
        }
    ),
    (
        'unknown key in nested dictionary',
        {
            'a': {
                'b': {
                    'c': 'd'
                }
            }
        },
        ('a', 'd'),
        None
    ),
    (
        'valid nested value',
        {
            'a': {
                'b': {
                    'c': 'd'
                }
            }
        },
        ('a', 'b', 'c'),
        'd'
    ),
    (
        'last key not a dict',
        {
            'a': {
                'b': {
                    'c': 'd'
                }
            }
        },
        ('a', 'b', 'c', 'd'),
        None
    ),
    (
        'value not a dictionary',
        {
            'a': {
                'b': {
                    'c': 'd'
                }
            }
        },
        ('a', 'b', 'c', 'd', 'e'),
        None
    )
])
def test_dict_nested_value(test_name, dictionary, keys, expected):
    assert dict_nested_value(dictionary, *keys) == expected
