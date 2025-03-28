# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
import tempfile

from gluetool.utils import dump_yaml

from gluetool_modules_framework.helpers.hide_secrets import HideSecrets
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module, patch_shared

from mock import MagicMock

ASSETS_DIR = os.path.join('gluetool_modules_framework', 'tests', 'assets')


@pytest.fixture(name='module')
def fixture_module():
    _, module = create_module(HideSecrets)
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 5
    return module


FILE_CONTENTS = """Lorem ipsum dolor sit amet, {} consectetur adipiscing elit, sed do eiusmod tempor
incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco
laboris nisi ut aliquip exea commodo consequat. Duis aute{}irure dolor in reprehenderit in voluptate velit
esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa
qui officia deserunt mollit anim id est laborum.

{}
"""


LONG_SECRET = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip exea commodo consequat. Duis auteirure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa  ui officia deserunt mollit anim id est laborum."

SSH_KEY_SECRET = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAACFwAAAAdzc2gtcn
NhAAAAAwEAAQAAAgEAwbGBzDKGiZ/2VI6KjPcaWaF4mmPIVPDe+tJs4KiThR3HDskX/U0/
ACDlW+fVSZAPjFuoBxZ4GoAcOcYRAhcT0OoZnZwbij27Tdot9KGTuxWlTqFh4wIvDyMe2E
3b8/9lS0bTI5AAAAE2V4YW1wbGVAZXhhbXBsZS5jb20BAgMEBQYH
-----END OPENSSH PRIVATE KEY-----"""


@pytest.mark.parametrize('testing_farm_request', [
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'foo'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'very long secret'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': ';uname;'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'hello*world'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'hello+world'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'he[llo+wo]rld'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': '|bar|'})])),
    (MagicMock(environments_requested=[TestingEnvironment(
        secrets={'secret': r'\'\'\a\b\c\d!##$%^&**(a)=?`=":._-[0-9]+/'}
    )])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': r"''''"})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': r'\a\b\c\d\n'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={'secret': 'a\nb\nc'})])),
    (MagicMock(environments_requested=[TestingEnvironment(secrets={})])),
    (MagicMock(environments_requested=[TestingEnvironment(tmt={'environment': {'var': 'abc'}})])),
    (MagicMock(environments_requested=[TestingEnvironment(tmt={'environment': None})]))
])
def test_hide_secrets(monkeypatch, module, testing_farm_request):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module._config['search-path'] = tmpdir

        secret_values = []
        for environment in testing_farm_request.environments_requested:
            if environment.secrets:
                secret_values += [
                    secret_value.replace('\n', '\\n')
                    for secret_value in environment.secrets.values() if secret_value
                ]

        # add secret values from requests
        module.add_secrets(secret_values)

        # Create a file containing some secrets
        secret_value = secret_values[0] if len(secret_values) > 0 else 'no secret'
        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write(FILE_CONTENTS.format(*[secret_value]*3))

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*[secret_value]*3)

        # Replace all secrets with 'hidden'
        module.destroy()

        # Check all secrets are now 'hidden' or 'no secret' if there are no secrets
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*['hidden' if len(secret_values) > 0 else 'no secret']*3)

        #
        # We also need to check if it works for filenames with special characters
        #

        # Create a file containing some secrets
        secret_value = secret_values[0] if len(secret_values) > 0 else 'no secret'
        with open(os.path.join(tmpdir, 't"e st\'f[i]l*e.txt'), 'w') as f:
            f.write(FILE_CONTENTS.format(*[secret_value]*3))

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 't"e st\'f[i]l*e.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*[secret_value]*3)

        # Replace all secrets with 'hidden'
        module.destroy()

        # Check all secrets are now 'hidden' or 'no secret' if there are no secrets
        with open(os.path.join(tmpdir, 't"e st\'f[i]l*e.txt'), 'r') as f:
            assert f.read() == FILE_CONTENTS.format(*['hidden' if len(secret_values) > 0 else 'no secret']*3)


def test_hide_secrets_yaml_dump_long_string(monkeypatch, module):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        testing_farm_request = MagicMock(
            environments_requested=[TestingEnvironment(secrets={'secret': LONG_SECRET})])

        module._config['search-path'] = tmpdir

        # add secret values from requests
        module.add_secrets([LONG_SECRET])

        dump_yaml(
            {
                'test': 'foo',
                'secret': LONG_SECRET
            },
            os.path.join(tmpdir, 'testfile.yml')
        )

        # Replace all secrets with 'hidden'
        module.destroy()

        with open(os.path.join(tmpdir, 'testfile.yml'), 'r') as f:
            assert f.read() == 'test: foo\nsecret: hidden\n'


# In tmt verbose log multiline secrets have indentation and we need to make sure
# that we hide them correctly, e.g.
# SSH_KEY_SECRET:
#    -----BEGIN OPENSSH PRIVATE KEY-----
#    b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAACFwAAAAdzc2gtcn
#    NhAAAAAwEAAQAAAgEAwbGBzDKGiZ/2VI6KjPcaWaF4mmPIVPDe+tJs4KiThR3HDskX/U0/
#    ACDlW+fVSZAPjFuoBxZ4GoAcOcYRAhcT0OoZnZwbij27Tdot9KGTuxWlTqFh4wIvDyMe2E
#    3b8/9lS0bTI5AAAAE2V4YW1wbGVAZXhhbXBsZS5jb20BAgMEBQYH
#    -----END OPENSSH PRIVATE KEY-----
def test_hide_secrets_multiline_indentation(monkeypatch, module):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module._config['search-path'] = tmpdir

        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write("SSH_KEY_SECRET: {}".format(SSH_KEY_SECRET.replace('\n', '\n    ')))
            f.flush()

        testing_farm_request = MagicMock(
            environments_requested=[TestingEnvironment(secrets={'SSH_KEY_SECRET': SSH_KEY_SECRET})])

        module._config['search-path'] = tmpdir

        module.add_secrets([SSH_KEY_SECRET])
        module.destroy()

        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == 'SSH_KEY_SECRET: hidden'


def test_hide_secrets_multiple(monkeypatch, module):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        testing_farm_request = MagicMock(environments_requested=[
            TestingEnvironment(secrets={'secret1': 'foo', 'secret2': 'bar'}),
            TestingEnvironment(secrets={'secret3': 'baz'})
        ])
        file_contents = 'foo hello bar world baz'
        file_contents_censored = 'hidden hello hidden world hidden'

        module._config['search-path'] = tmpdir
        patch_shared(monkeypatch, module, {
            'testing_farm_request': testing_farm_request
        })

        # add secret values from request
        module.add_secrets([
            value
            for environment in testing_farm_request.environments_requested
            for value in environment.secrets.values() if value
        ])

        # Create a file containing some secrets
        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write(file_contents)

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == file_contents

        # Replace all secrets with 'hidden'
        module.destroy()

        # Check all secrets are now 'hidden'
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == file_contents_censored


def test_hide_secrets_argument(monkeypatch, module):
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        testing_farm_request = MagicMock(environments_requested=[
            TestingEnvironment(secrets={'secret1': 'foo', 'secret2': 'bar'}),
            TestingEnvironment(secrets={'secret3': 'baz'})
        ])
        file_contents = 'foo hello bar world baz'
        file_contents_censored = 'hidden hello hidden world hidden'

        module._config['search-path'] = "not a real path"
        patch_shared(monkeypatch, module, {
            'testing_farm_request': testing_farm_request
        })

        # add secret values from request
        module.add_secrets([
            value
            for environment in testing_farm_request.environments_requested
            for value in environment.secrets.values() if value
        ])

        # Create a file containing some secrets
        with open(os.path.join(tmpdir, 'testfile.txt'), 'w') as f:
            f.write(file_contents)

        # Check the file was created successfully
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == file_contents

        # Replace all secrets with 'hidden'
        module.hide_secrets(search_path=tmpdir)

        # Check all secrets are now 'hidden'
        with open(os.path.join(tmpdir, 'testfile.txt'), 'r') as f:
            assert f.read() == file_contents_censored
