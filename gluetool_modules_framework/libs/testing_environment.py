# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool
from gluetool.utils import normalize_multistring_option

import gluetool_modules_framework.libs

import ast

from dataclasses import dataclass

# Type annotations
from typing import Any, Dict, List, Optional, Union, Tuple  # noqa


ComposeType = Union[str, gluetool_modules_framework.libs._UniqObject]
ArchType = Union[str, gluetool_modules_framework.libs._UniqObject]
SnapshotsType = bool


@dataclass
class TestingEnvironment(object):
    """
    To specify what environment should provisioner provide when asked for guest(s), one needs to
    describe attributes of such environment. It's up to provisioning modules to decode the information,
    and provision guest that would - according to their best knowledge - satisfy the request.

    Follows :doc:`Testing Environment Protocol </protocols/testing-environment>`.

    .. note::

       This is effectively a work in progress - we need to separate environments from provisioners
       and test runners, and that would let us make modules less dependant on the implementation
       of guests.

    :param str compose: Identification of the compose to be used for testing. It can be pretty much
        any string value, its purpose is to allow provisioning modules to chose the best distro/image/etc.
        suitable for the job. It will depend on what modules are connected in the pipeline, how they are
        configured and other factors. E.g. when dealing with ``workflow-tomorrow``, it can carry a tree
        name as known to Beaker, ``RHEL-7.5-updates-20180724.1`` or ``RHEL-6.10``; the provisioner should
        then deduce what guest configuration (arch & distro, arch & OpenStack image, and so on) would satisfy
        such request.
    :param str arch: Architecture that should be used for testing.
    :param bool snapshots: Choose a pool with snapshots support
    :param str pool: Name of the infrastructure pool to use.
    :param dict variables: Environment variables provided by the user.
    :param dict secrets: Environment variables provided by the user which should be hidden in outputs.
    :param list artifacts: Additional artifacts to install in the test environment.
    :param dict hardware: Test environment hardware specification.
    :param dict settings: Various environment settings or tweaks.
    :param dict tmt: Special environment settings for tmt tool.
    """

    arch: Optional[ArchType] = None
    compose: Optional[ComposeType] = None
    snapshots: SnapshotsType = False
    pool: Optional[str] = None
    variables: Optional[Dict[str, str]] = None
    secrets: Optional[Dict[str, str]] = None
    artifacts: Optional[List[Dict[str, Any]]] = None
    hardware: Optional[Dict[str, Any]] = None
    settings: Optional[Dict[str, Any]] = None
    tmt: Optional[Dict[str, Any]] = None

    # Make special values available to templates, they are now reachable as class variables
    # of each instance.
    ANY = gluetool_modules_framework.libs.ANY

    _fields = ('arch', 'compose', 'snapshots', 'variables', 'secrets', 'artifacts', 'hardware', 'settings', 'tmt')

    def __str__(self):
        # type: () -> str

        return self.serialize_to_string(hide_secrets=True, show_none_fields=False)

    def __repr__(self):
        # type: () -> str

        return '<TestingEnvironment({})>'.format(str(self))

    def __eq__(self, other):
        # type: (Any) -> bool

        if not isinstance(other, TestingEnvironment):
            return False

        return all([getattr(self, field) == getattr(other, field) for field in self._fields])

    def __hash__(self):
        # type: () -> int

        return hash(tuple([getattr(self, field) for field in self._fields]))

    def _serialize_get_fields(self, hide_secrets, show_none_fields):
        # type: (bool, bool) -> List[Tuple[str, Any]]
        fields = []
        for field_name in sorted(self._fields):
            field_value = getattr(self, field_name)
            if not show_none_fields and field_value is None:
                continue

            if hide_secrets and field_name == 'secrets' and field_value is not None:
                field_value = '******'
            fields.append((field_name, field_value))

        return fields

    def serialize_to_string(self, hide_secrets=True, show_none_fields=False):
        # type: (bool, bool) -> str
        """
        Serialize testing environment to comma-separated list of keys and their values, representing
        the environment.

        :param bool hide_secrets: show secret values in the resulting output as '******'
        :param bool show_none_fields: do not show values which are None
        :rtype: str
        :returns: testing environemnt properties in ``key1=value1,...`` form.
        """

        return ','.join([
            '{}={}'.format(name, value) for name, value in self._serialize_get_fields(hide_secrets, show_none_fields)
        ])

    def serialize_to_json(self, hide_secrets=True, show_none_fields=False):
        # type: (bool, bool) -> Dict[str, Any]
        """
        Serialize testing environment to a JSON dictionary.

        :param bool hide_secrets: show secret values in the resulting output as '******'
        :param bool show_none_fields: do not show values which are None
        :rtype: dict(str, object)
        """

        return {name: value for name, value in self._serialize_get_fields(hide_secrets, show_none_fields)}

    @classmethod
    def _assert_env_properties(cls, env_properties):
        # type: (List[str]) -> None

        for env_property in env_properties:
            if env_property in cls._fields:
                continue

            raise gluetool.GlueError("Testing environment does not have property '{}'".format(env_property))

    @classmethod
    def unserialize_from_string(cls, serialized):
        # type: (str) -> TestingEnvironment
        """
        Construct a testing environment from a comma-separated list of key and their values.

        :param str serialized: testing environment properties in ``key1=value1,...`` form.
        :rtype: TestingEnvironment
        """

        normalized = normalize_multistring_option(serialized)

        env_properties = {
            key.strip(): value.strip() for key, value in [
                env_property.split('=') for env_property in normalized
            ]
        }  # type: Dict[str, Any]

        cls._assert_env_properties(list(env_properties.keys()))

        if 'snapshots' in env_properties:
            env_properties['snapshots'] = gluetool.utils.normalize_bool_option(env_properties['snapshots'])

        for property in ['variables', 'secrets', 'artifacts', 'hardware', 'settings', 'tmt']:
            if property in env_properties:
                env_properties[property] = ast.literal_eval(env_properties[property])

        return TestingEnvironment(**env_properties)

    @classmethod
    def unserialize_from_json(cls, serialized):
        # type: (Dict[str, Any]) -> TestingEnvironment
        """
        Construct a testing environment from a JSON representation of fields and their values.

        :param dict(str, object) serialized: testing environment properties in a dictionary.
        :rtype: TestingEnvironment
        """

        cls._assert_env_properties(list(serialized.keys()))

        return TestingEnvironment(**serialized)

    def clone(self, **kwargs):
        # type: (**Any) -> TestingEnvironment
        """
        Create - possibly modified - copy of the environment.

        :param dict kwargs: if specified, each keyword argument represents a property of the environment,
            and it is applied after making a copy, therefore overwriting the original property of the copied
            environment.
        """

        model = self.serialize_to_json(hide_secrets=False)

        model.update(kwargs)

        return self.unserialize_from_json(model)
