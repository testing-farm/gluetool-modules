# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
import tempfile

from gluetool_modules_framework.helpers.hide_secrets import HideSecrets
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module, patch_shared

from mock import MagicMock

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


@pytest.fixture(name='module')
def fixture_module():
    _, module = create_module(HideSecrets)
    return module


FILE_CONTENTS = """Lorem ipsum dolor sit amet, {} consectetur adipiscing elit, sed do eiusmod tempor
incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco
laboris nisi ut aliquip exea commodo consequat. Duis aute{}irure dolor in reprehenderit in voluptate velit
esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa
qui officia deserunt mollit anim id est laborum.

{}
"""


@pytest.mark.parametrize('testing_farm_request', [
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'foo'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'very long secret'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': ';uname;'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'hello*world'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': '|bar|'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={})]))
])
def test_hide_secrets(monkeypatch, module, testing_farm_request):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module._config['search-path'] = tmpdir
        patch_shared(monkeypatch, module, {
            'testing_farm_request': testing_farm_request
        })

        secret_values = []
        for environment in testing_farm_request.environments_requested:
            if environment.secrets:
                secret_values += [secret_value for secret_value in environment.secrets.values() if secret_value]

        # Create a file containing some secrets
        secret_value = secret_values[0] if len(secret_values) > 0 else 'no secret'
        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write(FILE_CONTENTS.format(*[secret_value]*3))

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*[secret_value]*3)

        # Replace all secrets with '*****'
        module.destroy()

        # Check all secrets are now '*****' or 'no secret' if there are no secrets
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*['*****' if len(secret_values) > 0 else 'no secret']*3)


def test_hide_secrets_multiple(monkeypatch, module):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        testing_farm_request = MagicMock(environments_requested=[
            TestingEnvironment(secrets={'secret1': 'foo', 'secret2': 'bar'}),
            TestingEnvironment(secrets={'secret3': 'baz'})
        ])
        file_contents = 'foo hello bar world baz'
        file_contents_censored = '***** hello ***** world *****'

        module._config['search-path'] = tmpdir
        patch_shared(monkeypatch, module, {
            'testing_farm_request': testing_farm_request
        })

        # Create a file containing some secrets
        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write(file_contents)

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == file_contents

        # Replace all secrets with '*****'
        module.destroy()

        # Check all secrets are now '*****'
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == file_contents_censored
