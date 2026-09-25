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


@pytest.fixture(name='implementation', params=['sed', 'stream'])
def fixture_implementation(request):
    return request.param


@pytest.fixture(name='module')
def fixture_module(implementation):
    _, module = create_module(HideSecrets)
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 5
    module._config['implementation'] = implementation
    return module


@pytest.fixture(name='stream_module')
def fixture_stream_module():
    # For the guarantees only the 'stream' implementation makes, hence not parametrized.
    _, module = create_module(HideSecrets)
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 5
    module._config['implementation'] = 'stream'
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


@pytest.mark.parametrize('secret, contents', [
    # POSIX '[[:space:]]', which the sed implementation pads its line breaks with, includes
    # '\n' - so a multiline secret is matched no matter how much whitespace, blank lines
    # included, ends up between its lines.
    ('alpha-secret\nbeta-secret', 'X: alpha-secret\nbeta-secret :Y'),
    ('alpha-secret\nbeta-secret', 'X: alpha-secret\n    beta-secret :Y'),
    ('alpha-secret\nbeta-secret', 'X: alpha-secret\n\nbeta-secret :Y'),
    ('alpha-secret\nbeta-secret', 'X: alpha-secret\n  \n   beta-secret :Y'),
    # Secrets whose own lines are empty or blank. Worth their own cases: these are the ones
    # whose compiled pattern has no literal to start scanning from.
    ('\nbeta-secret', 'X:\nbeta-secret :Y'),
    (' \nbeta-secret', 'X:  \nbeta-secret :Y'),
    ('alpha-secret\n\nbeta-secret', 'X: alpha-secret\n\nbeta-secret :Y'),
    ('alpha-secret\n', 'X: alpha-secret\n :Y'),
])
def test_hide_secrets_multiline_shapes(secret, contents):
    # Redaction coverage has to be identical under both implementations - this is a security
    # control and 'stream' is meant to be a drop-in replacement for 'sed', so rather than
    # spell out an expected result, run both and require they agree byte for byte.
    outputs = {}

    for implementation in ('sed', 'stream'):
        _, module = create_module(HideSecrets)
        module._config['retry-tick'] = 1
        module._config['retry-timeout'] = 5
        module._config['implementation'] = implementation
        module.add_secrets([secret])

        with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
            path = os.path.join(tmpdir, 'testfile.txt')
            with open(path, 'w', newline='') as f:
                f.write(contents)

            module.hide_secrets(search_path=tmpdir)

            with open(path, 'r', newline='') as f:
                outputs[implementation] = f.read()

    assert outputs['sed'] == outputs['stream']

    # ...and that they agree on having actually hidden it.
    assert 'hidden' in outputs['stream']

    for line in secret.split('\n'):
        if line.strip():
            assert line.strip() not in outputs['stream']


def test_hide_secrets_does_not_follow_symlinks(module):
    # 'find' defaults to '-P', so 'find <path> -type f' never yields a symlink - not even when
    # the symlink is the starting point. Both implementations must leave them alone, and in
    # particular must not replace a symlink with a regular file.
    secret = 'symlinked-secret-value'

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module.add_secrets([secret])

        outside = os.path.join(tmpdir, 'outside.txt')
        with open(outside, 'w') as f:
            f.write('secret is {}\n'.format(secret))

        search_path = os.path.join(tmpdir, 'search')
        os.mkdir(search_path)

        link = os.path.join(search_path, 'link.txt')
        os.symlink(outside, link)

        regular = os.path.join(search_path, 'regular.txt')
        with open(regular, 'w') as f:
            f.write('secret is {}\n'.format(secret))

        module.hide_secrets(search_path=search_path)

        # The regular file is redacted, the symlink is still a symlink and its target intact.
        with open(regular, 'r') as f:
            assert f.read() == 'secret is hidden\n'

        assert os.path.islink(link)

        with open(outside, 'r') as f:
            assert f.read() == 'secret is {}\n'.format(secret)

        # Same when the symlink itself is the search path.
        module.hide_secrets(search_path=link)

        assert os.path.islink(link)

        with open(outside, 'r') as f:
            assert f.read() == 'secret is {}\n'.format(secret)


def test_hide_secrets_binary_with_nul_bytes(module):
    # The legacy sed implementation uses '-z' (NUL-separated records); make sure NUL bytes
    # elsewhere in the file do not prevent a secret from being found and hidden, regardless
    # of implementation.
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        secret = 'binarysecretvalue'
        module._config['search-path'] = tmpdir
        module.add_secrets([secret])

        path = os.path.join(tmpdir, 'binary.dat')
        content = b'\x00\x01' + secret.encode() + b'\x00\x02\xff' + b'tail\x00'
        with open(path, 'wb') as f:
            f.write(content)

        module.destroy()

        with open(path, 'rb') as f:
            result = f.read()

        assert secret.encode() not in result
        assert b'hidden' in result


def test_hide_secrets_large_file_without_secrets_is_unchanged(module):
    # A file bigger than the stream implementation's default chunk size, containing none of
    # the configured secrets, must come out byte-identical.
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module._config['search-path'] = tmpdir
        module.add_secrets(['a-secret-that-does-not-appear-anywhere-in-the-file'])

        path = os.path.join(tmpdir, 'large.log')
        line = 'just a regular ordinary log line with no secrets in it at all\n'
        content = line * 20000  # a little over 1 MiB

        with open(path, 'w') as f:
            f.write(content)

        module.destroy()

        with open(path, 'r') as f:
            assert f.read() == content


def test_hide_secrets_stream_clean_file_not_modified(stream_module):
    # Only the 'stream' implementation guarantees this: the legacy sed path always rewrites
    # (and thus always changes the inode of) every file it is pointed at, whether or not it
    # actually matched anything.
    module = stream_module

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module._config['search-path'] = tmpdir
        module.add_secrets(['a-secret-that-is-not-present-here'])

        path = os.path.join(tmpdir, 'clean.txt')
        with open(path, 'w') as f:
            f.write('nothing interesting here\n')

        before = os.stat(path)

        module.destroy()

        after = os.stat(path)

        assert before.st_ino == after.st_ino
        assert before.st_mtime_ns == after.st_mtime_ns


def test_hide_secrets_stream_large_file_no_secrets_no_leftovers(stream_module):
    # A file bigger than the default chunk size with no matching secrets must not be rewritten
    # at all - verified here by making sure no temporary file is left behind.
    module = stream_module
    module.add_secrets(['a-secret-that-is-not-present'])

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'nosecret.log')
        content = 'just some ordinary log output\n' * 50000

        with open(path, 'w') as f:
            f.write(content)

        module.hide_secrets(search_path=tmpdir)

        assert os.listdir(tmpdir) == ['nosecret.log']

        with open(path, 'r') as f:
            assert f.read() == content


@pytest.mark.parametrize('secret, needle, separator', [
    # single-line secret: the match itself can straddle the boundary at any offset.
    ('topsecret-0123456789', 'topsecret-0123456789', '\n'),
    # multiline secret: the line-joint (the widest possible match) must also survive landing
    # on a chunk boundary.
    ('line-one-of-the-secret\nline-two-of-the-secret', 'line-one-of-the-secret', '\n---\n'),
])
def test_hide_secrets_stream_chunk_boundary(stream_module, secret, needle, separator):
    # The highest-risk correctness case for the streaming implementation: a secret must be
    # found and redacted no matter where a chunk boundary happens to fall across it. Force a
    # tiny chunk size (well below the secret's length) and place the secret at every possible
    # offset relative to that chunk size.
    module = stream_module
    module.add_secrets([secret])

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'boundary.txt')

        offsets = list(range(24))
        content = separator.join('x' * n + secret + 'y' * 3 for n in offsets)
        with open(path, 'w') as f:
            f.write(content)

        matchers = module._build_stream_matchers()
        assert matchers is not None
        # Override the auto-computed chunk size: we want many small reads to genuinely
        # exercise the carry-over/overlap logic, regardless of what a real deployment would use.
        matchers.chunk_size = 8

        module._scrub_file(path, matchers)

        with open(path, 'r') as f:
            result = f.read()

        assert needle not in result
        assert result.count('hidden') == len(offsets)
