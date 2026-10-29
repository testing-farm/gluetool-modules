# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import functools
import pytest
import os
import re
import shutil
import subprocess
import tempfile
import tracemalloc

import gluetool
from gluetool.utils import dump_yaml
from hypothesis import assume, event, given, settings, strategies as st

import gluetool_modules_framework.helpers.hide_secrets as hide_secrets_module
from gluetool_modules_framework.helpers.hide_secrets import HideSecrets
from gluetool_modules_framework.libs.testing_environment import TestingEnvironment

from . import create_module, patch_shared

from mock import MagicMock, patch

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
    # A secret with a blank line of its own.
    ('alpha-secret\n\nbeta-secret', 'X: alpha-secret\n\nbeta-secret :Y'),
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


@pytest.mark.parametrize('secret, contents, expected', [
    # Whitespace around a multiline secret is not part of it. The sed implementation requires
    # it and redacts it too, 'stream' leaves it in place, and finds the secret without it.
    ('\nbeta-secret', 'X:\nbeta-secret :Y', 'X:\nhidden :Y'),
    (' \nbeta-secret', 'X:  \nbeta-secret :Y', 'X:  \nhidden :Y'),
    (' \nbeta-secret', 'X:beta-secret :Y', 'X:hidden :Y'),
    ('alpha-secret\n', 'X: alpha-secret\n :Y', 'X: hidden\n :Y'),
    ('\n  alpha-secret\nbeta-secret\n', 'X: alpha-secret\nbeta-secret :Y', 'X: hidden :Y'),
    # A secret of whitespace only is ignored, rather than redacting every line break.
    ('\n \n', 'X:\n \n:Y', 'X:\n \n:Y'),
])
def test_hide_secrets_stream_multiline_edges(stream_module, secret, contents, expected):
    module = stream_module
    module.add_secrets([secret])

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'testfile.txt')
        with open(path, 'w', newline='') as f:
            f.write(contents)

        module.hide_secrets(search_path=tmpdir)

        with open(path, 'r', newline='') as f:
            assert f.read() == expected


@pytest.mark.parametrize('gap', [
    ' ' * 300,
    '\n' + ' ' * 4096,
    '\n' * 1000,
    # More than one read of the 'stream' implementation.
    '\n' + ' ' * (2 * 1024 * 1024),
], ids=['300-spaces', '4096-spaces', '1000-newlines', '2MiB-spaces'])
def test_hide_secrets_multiline_any_indentation(module, gap):
    # However much whitespace separates the lines of a multiline secret, it is hidden.
    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module.add_secrets(['alpha-secret\nbeta-secret'])

        path = os.path.join(tmpdir, 'testfile.txt')
        with open(path, 'w') as f:
            f.write('X: alpha-secret\n' + gap + 'beta-secret :Y')

        module.hide_secrets(search_path=tmpdir)

        with open(path, 'r') as f:
            assert f.read() == 'X: hidden :Y'


def test_hide_secrets_multiline_blank_lines_no_backtracking(module):
    # A multiline secret with blank lines in it, next to plenty of whitespace which in the end
    # does not match. A pattern with more than one way to match the same whitespace takes
    # exponential time here.
    secret = 'alpha-secret\n\n\nbeta-secret'
    near_miss = 'alpha-secret' + ' \n' * 5000 + 'x beta-secret\n'

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module.add_secrets([secret])

        path = os.path.join(tmpdir, 'testfile.txt')
        with open(path, 'w') as f:
            f.write(near_miss + 'alpha-secret\n\n\n   beta-secret :Y')

        module.hide_secrets(search_path=tmpdir)

        with open(path, 'r') as f:
            assert f.read() == near_miss + 'hidden :Y'


@pytest.mark.parametrize('secrets, occurrence', [
    # A shorter secret inside a longer one must not leave the rest of the longer one visible.
    (['ABCDEFGHIJ', 'CD'], 'ABCDEFGHIJ'),
    (['alpha-secret\nbeta-secret', 'secret'], 'alpha-secret\n    beta-secret'),
    # Overlapping secrets are hidden together.
    (['XAB', 'ABCDEF'], 'XABCDEF'),
    # A secret which is a part of the word 'hidden' must not match the redacted text.
    (['topsecret', 'dde'], 'topsecret'),
], ids=['nested', 'nested-multiline', 'overlapping', 'hidden-substring'])
@pytest.mark.parametrize('chunk_size', [None, 8], ids=['default-chunk', 'tiny-chunk'])
def test_hide_secrets_stream_overlapping_secrets(stream_module, secrets, occurrence, chunk_size):
    # Wherever a read boundary falls, the result is the same as for the whole file at once.
    module = stream_module
    module.add_secrets(secrets)

    offsets = list(range(24))

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'boundary.txt')

        with open(path, 'w') as f:
            f.write('\n'.join('x' * n + occurrence + 'y' * 3 for n in offsets))

        matchers = module._build_stream_matchers()
        assert matchers is not None

        if chunk_size is not None:
            matchers.chunk_size = chunk_size

        module._scrub_file(path, matchers)

        with open(path, 'r') as f:
            assert f.read() == '\n'.join('x' * n + 'hidden' + 'y' * 3 for n in offsets)


def test_hide_secrets_stream_dense_overlaps(stream_module):
    # Secrets overlapping each other at every byte of the file, over two million matches in
    # 32 KiB. Memory use must not grow with the number of matches.
    module = stream_module
    module.add_secrets(['a' * n for n in range(1, 65)])

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'dense.txt')
        with open(path, 'w') as f:
            f.write('a' * 32 * 1024)

        tracemalloc.start()

        try:
            module.hide_secrets(search_path=tmpdir)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < 8 * 1024 * 1024

        with open(path, 'r') as f:
            assert f.read() == 'hidden'


@pytest.mark.parametrize('space', [' ', '\u3000', ' \u3000\u2028\t'], ids=['ascii', 'ideographic', 'mixed'])
def test_hide_secrets_stream_long_carry_memory(stream_module, space):
    # Megabytes of whitespace between the lines of a secret, held back from one read to the
    # next. Memory use must stay a small multiple of what is held back, whatever whitespace it is.
    module = stream_module
    module.add_secrets(['alpha-secret\nbeta-secret'])

    gap = space * (4 * 1024 * 1024 // len(space.encode('utf-8')))

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'testfile.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('X: alpha-secret\n' + gap + 'beta-secret :Y')

        tracemalloc.start()

        try:
            module.hide_secrets(search_path=tmpdir)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < 32 * 1024 * 1024

        with open(path, 'r', encoding='utf-8') as f:
            assert f.read() == 'X: hidden :Y'


def test_hide_secrets_stream_carry_limit(monkeypatch, stream_module):
    # Holding back a long stretch of whitespace from one read to the next is bounded, and
    # past the bound, a file which may contain a secret split by it fails, rather than leaks.
    module = stream_module
    module.add_secrets(['alpha-secret\nbeta-secret'])

    monkeypatch.setattr(hide_secrets_module, 'STREAM_MAX_CARRY', 1024)

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        blank = os.path.join(tmpdir, 'blank.txt')
        with open(blank, 'w') as f:
            f.write('start\n' + ' ' * 4096 + '\nend')

        complete = os.path.join(tmpdir, 'complete.txt')
        with open(complete, 'w') as f:
            f.write('alpha-secret\nbeta-secret' + ' ' * 4096)

        # No secret starts in the whitespace, the file stays as it is.
        module.hide_secrets(search_path=blank)

        with open(blank, 'r') as f:
            assert f.read() == 'start\n' + ' ' * 4096 + '\nend'

        # The secret is complete before the whitespace starts.
        module.hide_secrets(search_path=complete)

        with open(complete, 'r') as f:
            assert f.read() == 'hidden' + ' ' * 4096

        split = os.path.join(tmpdir, 'split.txt')
        with open(split, 'w') as f:
            f.write('alpha-secret\n' + ' ' * 4096 + 'beta')

        with pytest.raises(gluetool.GlueError, match='secrets could be leaked'):
            module.hide_secrets(search_path=split)


def _tree_with_secrets(tmpdir, secret, count):
    paths = []

    for index in range(count):
        path = os.path.join(tmpdir, 'file-{}.txt'.format(index))
        with open(path, 'w') as f:
            f.write('token={}\n'.format(secret))
        paths.append(path)

    return paths


def test_hide_secrets_stream_carry_limit_scrubs_other_files(monkeypatch, stream_module):
    # A file hitting the carry limit fails, but only after every other file is scrubbed, as
    # 'xargs' does for sed. The first line of a PEM key is public, anyone can produce such a file.
    module = stream_module
    module.add_secrets(['alpha-secret\nbeta-secret', 'token-secret'])

    monkeypatch.setattr(hide_secrets_module, 'STREAM_MAX_CARRY', 1024)

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        split = os.path.join(tmpdir, 'split.txt')
        with open(split, 'w') as f:
            f.write('alpha-secret\n' + ' ' * 4096 + 'beta')

        paths = _tree_with_secrets(tmpdir, 'token-secret', 6)

        with pytest.raises(gluetool.GlueError, match=r"in '{}', secrets could be leaked".format(re.escape(split))):
            module.hide_secrets(search_path=tmpdir)

        for path in paths:
            with open(path, 'r') as f:
                assert f.read() == 'token=hidden\n'


def test_hide_secrets_stream_retries_failed_files_only(stream_module):
    # A file failing with OSError is retried alone, the files already scrubbed are not read again.
    module = stream_module
    module.add_secrets(['token-secret'])

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        paths = _tree_with_secrets(tmpdir, 'token-secret', 4)
        flaky = paths[0]

        scrub_file = module._scrub_file
        calls = []

        def _scrub_file(path, matchers):
            calls.append(path)

            if path == flaky and calls.count(flaky) == 1:
                raise OSError('flaky')

            scrub_file(path, matchers)

        with patch.object(module, '_scrub_file', side_effect=_scrub_file):
            module.hide_secrets(search_path=tmpdir)

        assert sorted(calls) == sorted(paths + [flaky])

        for path in paths:
            with open(path, 'r') as f:
                assert f.read() == 'token=hidden\n'


def test_hide_secrets_stream_failing_file_scrubs_other_files(stream_module):
    # A file failing with OSError until the timeout fails the call, the other files are scrubbed.
    module = stream_module
    module._config['retry-timeout'] = 1
    module.add_secrets(['token-secret'])

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        paths = _tree_with_secrets(tmpdir, 'token-secret', 4)
        broken = paths[0]

        scrub_file = module._scrub_file

        def _scrub_file(path, matchers):
            if path == broken:
                raise OSError('broken')

            scrub_file(path, matchers)

        with patch.object(module, '_scrub_file', side_effect=_scrub_file):
            with pytest.raises(gluetool.GlueError, match=r"in '{}', secrets could be leaked".format(re.escape(broken))):
                module.hide_secrets(search_path=tmpdir)

        for path in paths[1:]:
            with open(path, 'r') as f:
                assert f.read() == 'token=hidden\n'


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


def test_hide_secrets_sanity(module):
    module.sanity()


@pytest.mark.parametrize('value', ['streaming', 'Stream', ''])
def test_hide_secrets_sanity_unknown_implementation(value):
    # A config file value skips the 'choices' check, a typo must not quietly run 'sed'.
    _, module = create_module(HideSecrets)
    module._config['implementation'] = value

    with pytest.raises(gluetool.GlueError, match="Unknown implementation '{}'".format(value)):
        module.sanity()


def test_hide_secrets_keeps_owner_and_group(module):
    # `sed -i` keeps the owner and group of a file it rewrites, as far as the user may set them.
    if os.geteuid() == 0:
        uid, gid = 12345, 12345
    else:
        groups = [group for group in os.getgroups() if group != os.getegid()]

        if not groups:
            pytest.skip('needs root, or a supplementary group')

        uid, gid = os.geteuid(), groups[0]

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module.add_secrets(['owned-secret'])

        path = os.path.join(tmpdir, 'owned.txt')
        with open(path, 'w') as f:
            f.write('secret is owned-secret\n')

        os.chown(path, uid, gid)
        os.chmod(path, 0o640)

        module.hide_secrets(search_path=tmpdir)

        with open(path, 'r') as f:
            assert f.read() == 'secret is hidden\n'

        st = os.stat(path)
        assert (st.st_uid, st.st_gid, st.st_mode & 0o7777) == (uid, gid, 0o640)


def test_hide_secrets_keeps_acl(module):
    # `sed -i` keeps the ACL of a file it rewrites.
    if shutil.which('setfacl') is None:
        pytest.skip('needs setfacl')

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        module.add_secrets(['acl-secret'])

        path = os.path.join(tmpdir, 'acl.txt')
        with open(path, 'w') as f:
            f.write('secret is acl-secret\n')

        if subprocess.run(['setfacl', '-m', 'u:12345:r', path], capture_output=True).returncode != 0:
            pytest.skip('filesystem does not support ACLs')

        acl = os.getxattr(path, 'system.posix_acl_access')

        module.hide_secrets(search_path=tmpdir)

        with open(path, 'r') as f:
            assert f.read() == 'secret is hidden\n'

        assert os.getxattr(path, 'system.posix_acl_access') == acl


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


# Every character Python considers whitespace, which is what the 'stream' implementation pads
# the line breaks of a multiline secret with.
UNICODE_WHITESPACE = [chr(code) for code in range(0x110000) if chr(code).isspace()]

# Characters which are not whitespace, but whose encoding starts like the encoding of one.
NEAR_WHITESPACE = ['\xa1', '\xc2', '\u1681', '\u2010', '\u202a', '\u2060', '\u3001', '\u20ac']

# The leading bytes of the multibyte whitespace characters, and bytes which cannot start one.
PARTIAL_BYTES = [
    b'\xc2', b'\xe1', b'\xe1\x9a', b'\xe2', b'\xe2\x80', b'\xe2\x81', b'\xe3', b'\xe3\x80', b'\x80', b'\xff'
]

# Mostly a few plain characters, so that secrets often show up in the contents, or a part of
# them does. Then whitespace and characters which look like it, which is where the 'stream'
# implementation is hard to get right, and now and then anything at all.
CHARACTERS = st.one_of(
    st.sampled_from('ab\n'),
    st.sampled_from('ab\n'),
    st.sampled_from(UNICODE_WHITESPACE),
    st.sampled_from(NEAR_WHITESPACE),
    st.characters(codec='utf-8'),
)


def _text(characters, max_size):
    # Unlike `st.text()`, which draws from all of its alphabet evenly, this keeps the weights
    # of `st.one_of()`.
    return st.lists(characters, max_size=max_size).map(''.join)


WHITESPACE = _text(st.sampled_from(UNICODE_WHITESPACE), max_size=4)


def _valid_utf8_characters():
    return (chr(code) for code in range(0x110000) if not 0xd800 <= code <= 0xdfff)


def _space_at(data, pos):
    """
    Return the length of the whitespace character at ``pos`` of ``data``, or 0.
    """

    for size in (1, 2, 3):
        try:
            char = data[pos:pos + size].decode('utf-8')
        except UnicodeDecodeError:
            continue

        return size if char.isspace() else 0

    return 0


def _reference_matches(secrets, data):
    """
    Return ``(start, end)`` of every match of every secret in ``data``, found the slow way: by
    trying each secret at every position.
    """

    matches = []

    for secret in secrets:
        if not secret:
            continue

        if '\n' in secret:
            secret = secret.strip()

            if not secret:
                continue

        pieces = [piece.encode('utf-8') for piece in re.split(r'\s*\n\s*', secret)]
        newlines = [run.count('\n') for run in re.findall(r'\s*\n\s*', secret)]

        for start in range(len(data)):
            if not data.startswith(pieces[0], start):
                continue

            pos = start + len(pieces[0])

            for count, piece in zip(newlines, pieces[1:]):
                # A piece starts with something other than whitespace, so the whitespace
                # before it is all of the whitespace there is.
                found = 0

                while True:
                    size = _space_at(data, pos)

                    if not size:
                        break

                    found += data[pos:pos + size] == b'\n'
                    pos += size

                if found < count or not data.startswith(piece, pos):
                    break

                pos += len(piece)

            else:
                matches.append((start, pos))

    return matches


def _reference_spans(secrets, data):
    """
    Return the spans of ``data`` the 'stream' implementation must redact: every match, with
    those overlapping each other merged.
    """

    spans = []

    for start, end in sorted(_reference_matches(secrets, data)):
        if spans and start < spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))

    return spans


def _redact(data, spans):
    pos, output = 0, b''

    for start, end in spans:
        output += data[pos:start] + b'hidden'
        pos = end

    return output + data[pos:]


@st.composite
def _occurrences(draw, secret):
    """
    The secret, as it may show up in a file: with any whitespace around its line breaks, as
    long as there are at least as many line breaks, and at times cut short.
    """

    if '\n' in secret:
        secret = secret.strip()

    pieces = re.split(r'\s*\n\s*', secret)
    occurrence = pieces[0]

    for run, piece in zip(re.findall(r'\s*\n\s*', secret), pieces[1:]):
        occurrence += '\n'.join(draw(WHITESPACE) for _ in range(run.count('\n') + 1)) + piece

    encoded = occurrence.encode('utf-8')

    return encoded[:draw(st.integers(0, len(encoded)))] if draw(st.booleans()) else encoded


@st.composite
def _contents(draw, secrets):
    return b''.join(draw(st.lists(st.one_of(
        st.sampled_from(secrets).flatmap(_occurrences),
        st.sampled_from(secrets).flatmap(_occurrences),
        WHITESPACE.map(lambda text: text.encode('utf-8')),
        _text(CHARACTERS, max_size=6).map(lambda text: text.encode('utf-8')),
        st.sampled_from(PARTIAL_BYTES),
        st.binary(max_size=3),
    ), max_size=12)))


@functools.lru_cache(maxsize=None)
def _cached_module(implementation):
    # Creating a module per example would make up most of the time hypothesis takes.
    _, module = create_module(HideSecrets)
    module._config['retry-tick'] = 1
    module._config['retry-timeout'] = 5
    module._config['implementation'] = implementation
    return module


@functools.lru_cache(maxsize=None)
def _sed_spaces():
    """
    Return the characters '[[:space:]]' matches for sed in the C.UTF-8 locale, the locale it
    runs in on workers, see `hide_secrets._SPACE`.
    """

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'characters.txt')

        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(char for char in _valid_utf8_characters() if char != '\n'))

        output = subprocess.run(
            ['sed', '-n', '/^[[:space:]]$/p', path],
            env=dict(os.environ, LC_ALL='C.UTF-8'),
            stdout=subprocess.PIPE,
            check=True
        ).stdout

    return frozenset(output.decode('utf-8').split('\n')[:-1]) | {'\n'}


def _runs(spans):
    runs = []

    for start, end in spans:
        if runs and runs[-1][1] == start:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((start, end))

    return runs


def test_hide_secrets_stream_whitespace_is_unicode_whitespace():
    # In valid UTF-8, the whitespace the 'stream' implementation matches is exactly the
    # characters `str.isspace()` accepts, nothing more, nothing less.
    space = re.compile(br'(?:%s)+' % hide_secrets_module._SPACE)
    space_not_newline = re.compile(br'(?:%s)+' % hide_secrets_module._SPACE_NOT_NEWLINE)

    encoded = [char.encode('utf-8') for char in _valid_utf8_characters()]
    data = b''.join(encoded)

    expected, pos = [], 0

    for char, char_encoded in zip(_valid_utf8_characters(), encoded):
        if char.isspace():
            expected.append((pos, pos + len(char_encoded), char))

        pos += len(char_encoded)

    assert [match.span() for match in space.finditer(data)] == _runs((start, end) for start, end, _ in expected)
    assert [match.span() for match in space_not_newline.finditer(data)] == _runs(
        (start, end) for start, end, char in expected if char != '\n'
    )

    # The rest of the whitespace `\s` already matches.
    assert sorted(hide_secrets_module._UNICODE_SPACES) == [
        char for char in UNICODE_WHITESPACE if not re.match(br'\s', char.encode('utf-8'))
    ]

    # What a secret counts as non-whitespace is exact.
    assert hide_secrets_module._ANY_SPACE.sub(b'', data) == b''.join(
        char_encoded for char, char_encoded in zip(_valid_utf8_characters(), encoded) if not char.isspace()
    )


def test_hide_secrets_stream_maybe_space():
    # Every byte of every whitespace character counts as possibly whitespace, so a character
    # cut off by the end of a read, or by the start of what `boundary()` looks at, does too.
    for char in UNICODE_WHITESPACE:
        assert set(char.encode('utf-8')) <= set(hide_secrets_module._MAYBE_SPACE)

    # And nothing else does, so that the bytes of other characters still count.
    assert set(hide_secrets_module._MAYBE_SPACE) == {
        byte for char in UNICODE_WHITESPACE for byte in char.encode('utf-8')
    }


def test_hide_secrets_sed_whitespace_is_unicode_whitespace():
    # The 'stream' implementation hides at least what 'sed' does only if everything sed counts
    # as whitespace, `str.isspace()` does too.
    sed_spaces = _sed_spaces()

    if '\u3000' not in sed_spaces:
        pytest.skip('sed has no C.UTF-8 locale here')

    assert sed_spaces <= set(UNICODE_WHITESPACE)


def _reference_nonspace(data):
    """
    Return the number of bytes of ``data`` which are not whitespace, reading it from its start,
    and counting the leading bytes of a whitespace character at its end as whitespace.
    """

    cut_off = {
        char.encode('utf-8')[:size] for char in UNICODE_WHITESPACE for size in range(1, len(char.encode('utf-8')))
    }

    count, pos = 0, 0

    while pos < len(data):
        size = _space_at(data, pos)

        if size:
            pos += size
        elif data[pos:] in cut_off:
            break
        else:
            count += 1
            pos += 1

    return count


@settings(max_examples=3000, deadline=None)
@given(
    contents=st.lists(st.one_of(
        st.sampled_from([b'a', b'b']),
        st.sampled_from(UNICODE_WHITESPACE + NEAR_WHITESPACE).map(lambda char: char.encode('utf-8')),
        st.sampled_from(PARTIAL_BYTES),
    ), max_size=40).map(b''.join),
    nonspace=st.integers(1, 8)
)
def test_hide_secrets_stream_nonspace_boundary(contents, nonspace):
    # A match of a multiline secret starts with the first byte of a character, and holds as many
    # non-whitespace bytes as the secret. When fewer follow a byte like that, the match may
    # continue in the next read, so the byte is not settled.
    matchers = hide_secrets_module._StreamMatchers(patterns=[hide_secrets_module._MultilinePattern(
        prefilter=b'a', first=b'a', pattern=re.compile(b'a'), nonspace=nonspace
    )])

    settled = matchers._nonspace_boundary(contents)

    for start in range(settled):
        if not 0x80 <= contents[start] <= 0xbf:
            assert _reference_nonspace(contents[start:]) >= nonspace


@settings(max_examples=1000, deadline=None)
@given(data=st.data())
def test_hide_secrets_stream_boundary(data):
    # Wherever a read ends, every match starting among the bytes `boundary()` reports as
    # settled ends within the bytes read so far.
    secrets = data.draw(st.lists(_text(CHARACTERS, max_size=10), min_size=1, max_size=3))
    contents = data.draw(_contents(secrets))

    module = _cached_module('stream')
    module._secrets = set(secrets)

    matchers = module._build_stream_matchers()
    assume(matchers is not None)

    matches = _reference_matches(secrets, contents)

    for size in range(len(contents) + 1):
        boundary = matchers.boundary(contents[:size])

        for start, end in matches:
            if start < boundary:
                assert end <= size


def test_hide_secrets_stream_boundary_cuts_into_whitespace(stream_module):
    # The tail of a read `boundary()` counts non-whitespace bytes in can start in the middle of
    # a multibyte whitespace character. Counting the bytes it cuts off as non-whitespace would
    # settle the 'a' below, and with it the first line of the secret, before 'd' shows up.
    module = stream_module
    module.add_secrets(['a\nbcd'])

    matchers = module._build_stream_matchers()
    assert matchers is not None
    assert matchers.nonspace == 4

    # The last 16 bytes, four times the non-whitespace bytes of the secret, start right after
    # the first byte of U+3000.
    window = 'a\u3000\n{}bc'.format(' ' * 11).encode('utf-8')
    assert window[-16:].startswith(b'\x80\x80')

    assert matchers.boundary(window) == 0

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'testfile.txt')

        with open(path, 'wb') as f:
            f.write(window + b'd')

        matchers.chunk_size = len(window)
        module._scrub_file(path, matchers)

        with open(path, 'rb') as f:
            assert f.read() == b'hidden'


@settings(max_examples=1500, deadline=None)
@given(data=st.data())
def test_hide_secrets_stream_matches_reference(data):
    # Whatever the secrets, the file and the size of reads are, 'stream' redacts exactly what
    # trying every secret at every position of the whole file finds - and leaves a file
    # with nothing to redact as it was.
    secrets = data.draw(st.lists(_text(CHARACTERS, max_size=10), min_size=1, max_size=3))
    contents = data.draw(_contents(secrets))
    chunk_size = data.draw(st.integers(1, 64))

    module = _cached_module('stream')
    module._secrets = set(secrets)

    spans = _reference_spans(secrets, contents)

    with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
        path = os.path.join(tmpdir, 'testfile.txt')

        with open(path, 'wb') as f:
            f.write(contents)

        inode = os.stat(path).st_ino

        matchers = module._build_stream_matchers()

        if matchers is not None:
            matchers.chunk_size = chunk_size
            module._scrub_file(path, matchers)

        with open(path, 'rb') as f:
            assert f.read() == _redact(contents, spans)

        if not spans:
            assert os.stat(path).st_ino == inode


@settings(max_examples=200, deadline=None)
@given(data=st.data())
def test_hide_secrets_stream_hides_what_sed_hides(data):
    # A multiline secret, indented with any whitespace: whenever 'sed' hides it, running as it
    # does on workers, 'stream' hides it the same way. 'stream' also hides it when indented
    # with whitespace sed does not know about.
    text = _text(
        st.one_of(st.sampled_from('ab'), st.sampled_from(NEAR_WHITESPACE), st.characters(
            codec='utf-8', exclude_characters='\n\x00'
        )),
        max_size=6
    )

    pieces = data.draw(st.lists(text.map(str.strip).filter(bool), min_size=2, max_size=4))
    newlines = data.draw(st.lists(st.integers(1, 3), min_size=len(pieces) - 1, max_size=len(pieces) - 1))
    # Whitespace sed knows about, or any.
    spaces = st.sampled_from(data.draw(st.sampled_from([sorted(_sed_spaces()), UNICODE_WHITESPACE])))
    runs = ['\n'.join(data.draw(_text(spaces, max_size=4)) for _ in range(count + 1)) for count in newlines]

    secret = pieces[0] + ''.join('\n' * count + piece for count, piece in zip(newlines, pieces[1:]))
    occurrence = pieces[0] + ''.join(run + piece for run, piece in zip(runs, pieces[1:]))
    contents = (data.draw(text) + occurrence + data.draw(text)).encode('utf-8')

    # Exactly one occurrence, so that both implementations hide it, or none does.
    spans = _reference_spans([secret], contents)
    assume(len(spans) == 1)

    outputs = {}

    for implementation in ('sed', 'stream'):
        module = _cached_module(implementation)
        module._secrets = {secret}

        with tempfile.TemporaryDirectory(prefix='hide_secrets', dir=ASSETS_DIR) as tmpdir:
            path = os.path.join(tmpdir, 'testfile.txt')

            with open(path, 'wb') as f:
                f.write(contents)

            with patch.dict(os.environ, {'LC_ALL': 'C.UTF-8'}):
                module.hide_secrets(search_path=tmpdir)

            with open(path, 'rb') as f:
                outputs[implementation] = f.read()

    assert outputs['stream'] == _redact(contents, spans)

    if set(''.join(runs)) <= _sed_spaces():
        event('sed hides it')
        assert outputs['sed'] == outputs['stream']
    else:
        event('only stream hides it')
        assert outputs['sed'] == contents
