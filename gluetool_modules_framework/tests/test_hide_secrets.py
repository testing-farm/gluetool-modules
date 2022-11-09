# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
import tempfile

from gluetool_modules_framework.helpers.hide_secrets import HideSecrets

from . import create_module, patch_shared


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


@pytest.mark.parametrize('secrets', [
    {'secret': 'foo'},
    {'secret': 'very long secret'},
    {'secret': ';uname;'},
    {'secret': 'hello*world'},
    {'secret': '|bar|'},
    {}
])
def test_hide_secret(monkeypatch, module, secrets):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module._config['search-path'] = tmpdir
        patch_shared(monkeypatch, module, {
            'user_secrets': secrets
        })

        # Create a file containing some secrets
        secret_value = list(secrets.values())[0] if len(secrets) > 0 else 'no secret'
        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write(FILE_CONTENTS.format(*[secret_value]*3))

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*[secret_value]*3)

        # Replace all secrets with '*****'
        module.destroy()

        # Check all secrets are now '*****' or 'no secret' if there are no secrets
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*['*****' if len(secrets) > 0 else 'no secret']*3)
