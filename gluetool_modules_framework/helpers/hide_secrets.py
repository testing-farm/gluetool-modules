# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re
import stat
import tempfile

import gluetool

from dataclasses import dataclass, field
from gluetool.result import Result

from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union  # noqa

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10
DEFAULT_IMPLEMENTATION = 'sed'

# Size of the read/write buffer used by the 'stream' implementation. Actual buffer size used
# at runtime is at least this large, and larger still if needed to keep it bigger than the
# overlap required by the longest configured secret, see `_build_stream_matchers`.
DEFAULT_STREAM_CHUNK_SIZE = 1 * 1024 * 1024

# Maximum amount of whitespace (including newlines) the 'stream' implementation tolerates
# between the lines of a multiline secret. Mirrors the intent of the legacy sed
# '[[:space:]]*' padding, but bounded, so the worst-case match length - and therefore the
# required chunk overlap - stays a fixed, computable number instead of "as much as the file
# happens to contain".
STREAM_MAX_INDENT = 256


@dataclass
class _MultilinePattern:
    """
    A secret spanning multiple lines, compiled to tolerate re-indentation at each line break,
    plus the longest of its lines.

    Every line of a multiline secret has to appear literally for the pattern to match, so
    looking for the longest one with `bytes.find` first is a cheap way to skip the regex on
    the overwhelming majority of chunks. `re` does this by itself when the pattern starts with
    a literal - but the moment it does not, which is what a secret whose first line is empty
    or a single space gives us, scanning collapses to a tenth of the throughput.
    """

    prefilter: bytes
    pattern: re.Pattern[bytes]

    def matches(self, data: bytes) -> bool:
        if self.prefilter and self.prefilter not in data:
            return False

        return self.pattern.search(data) is not None

    def redact(self, data: bytes) -> bytes:
        if self.prefilter and self.prefilter not in data:
            return data

        return self.pattern.sub(b'hidden', data)


@dataclass
class _StreamMatchers:
    """
    Compiled matchers used by the 'stream' implementation, plus the chunk size and overlap
    they require to correctly detect and redact secrets split across read buffers.
    """

    # Secrets with no newline, matched as plain bytes (no regex involved).
    literals: List[bytes] = field(default_factory=list)

    # Secrets spanning multiple lines.
    patterns: List[_MultilinePattern] = field(default_factory=list)

    # Number of trailing bytes of a processed chunk which must be carried over into the next
    # one, so that a match starting near the end of a chunk is never split across the boundary.
    overlap: int = 0

    # Number of bytes read from the file per iteration. `_build_stream_matchers` keeps this
    # larger than `overlap` so that each iteration reads more than it carries over; matching
    # stays correct below that, it just does more work per byte of progress.
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE

    def contains_match(self, data: bytes) -> bool:
        for literal in self.literals:
            if literal in data:
                return True

        return any(multiline.matches(data) for multiline in self.patterns)

    def redact(self, data: bytes) -> bytes:
        for literal in self.literals:
            if literal in data:
                data = data.replace(literal, b'hidden')

        for multiline in self.patterns:
            data = multiline.redact(data)

        return data


class HideSecrets(gluetool.Module):
    """
    Hide secrets from all files in the search path, by default
    current working directory.
    """

    name = 'hide-secrets'
    options = {
        'search-path': {
            'help': 'Path used to search for files (default: %(default)s)',
            'default': '.'
        },
        'implementation': {
            'help': """
                Implementation used to hide secrets. 'sed' shells out to `find`, `xargs` and
                `sed` for every call; it is battle-tested but reads whole files into memory,
                which can exhaust worker memory - and hard-fails above 2 GiB - on large files
                such as long-running guest console logs. 'stream' is a pure Python
                implementation which redacts files in constant memory with no size limit.
                (default: %(default)s)
            """,
            'choices': ('sed', 'stream'),
            'default': DEFAULT_IMPLEMENTATION,
        },
        'retry-tick': {
            'help': 'Timeout between retries for failed operations. (default: %(default)s)',
            'metavar': 'RETRY_TICK',
            'type': int,
            'default': DEFAULT_RETRY_TICK,
        },
        'retry-timeout': {
            'help': 'Timeout for retries in seconds. (default: %(default)s)',
            'metavar': 'RETRY_TIMEOUT',
            'type': int,
            'default': DEFAULT_RETRY_TIMEOUT,
        },
    }
    shared_functions = ['add_secrets', 'hide_secrets']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(HideSecrets, self).__init__(*args, **kwargs)
        self._secrets: Set[str] = set()

        # Used by the 'stream' implementation only: maps a file path to the (size, mtime_ns)
        # it had the last time it was scanned and found to contain none of the known secrets.
        # Lets repeated `hide_secrets()` calls - e.g. archive's parallel-archiving tick - skip
        # files that have not changed since they were last verified clean. Entries are keyed on
        # where the file lives for good, which is not necessarily where we scanned it, see
        # `_cache_key`.
        self._clean_files: Dict[str, Tuple[int, int]] = {}

    def add_secrets(self, secret: Union[str, List[str]]) -> None:
        new_values = set(secret) if isinstance(secret, list) else {secret}

        # A secret we have not seen before invalidates the whole "known clean" cache: any
        # previously-scanned file could contain it and was never checked against it.
        if not new_values.issubset(self._secrets):
            self._clean_files = {}

        self._secrets.update(new_values)

    def _iter_regular_files(self, search_path: str) -> Iterator[str]:
        """
        Mirror `find <path> -type f` semantics: symlinks are never followed - not the ones
        encountered while walking a directory, not symlinked directories, and not `search_path`
        itself. `find` defaults to '-P', under which a symlink is of type 'l' and never 'f',
        even when it is the starting point, so the sed implementation leaves those alone too.
        """
        if os.path.islink(search_path):
            return

        if os.path.isdir(search_path):
            for root, _, files in os.walk(search_path, followlinks=False):
                for name in files:
                    full_path = os.path.join(root, name)
                    if not os.path.islink(full_path) and os.path.isfile(full_path):
                        yield full_path

        elif os.path.isfile(search_path):
            yield search_path

    def _build_stream_matchers(self) -> Optional[_StreamMatchers]:
        literals: List[bytes] = []
        patterns: List[_MultilinePattern] = []
        max_match_len = 0

        # Mirrors the legacy sed implementation's '[[:space:]]*' padding around a newline,
        # bounded to a fixed maximum so the worst-case match length is a known quantity.
        # Python's `\s` on bytes is '[ \t\n\r\f\v]' - it has to keep matching '\n', exactly
        # like POSIX '[[:space:]]' does, otherwise a secret whose lines end up separated by a
        # blank line would be redacted by the sed implementation but not by this one.
        joiner = br'\s{0,%d}' % STREAM_MAX_INDENT
        line_break = joiner + b'\n' + joiner

        for secret in self._secrets:
            # Matches the existing sed-based behaviour: an empty secret is not something we
            # can meaningfully redact (and, unlike a targeted literal, would match everywhere).
            if not secret:
                continue

            encoded = secret.encode('utf-8', 'surrogateescape')
            lines = encoded.split(b'\n')

            if len(lines) == 1:
                literals.append(encoded)
                max_match_len = max(max_match_len, len(encoded))
                continue

            patterns.append(_MultilinePattern(
                prefilter=max(lines, key=len),
                pattern=re.compile(line_break.join(re.escape(line) for line in lines))
            ))

            match_len = sum(len(line) for line in lines) + (len(lines) - 1) * (2 * STREAM_MAX_INDENT + 1)
            max_match_len = max(max_match_len, match_len)

        if not literals and not patterns:
            return None

        # The overlap must cover the longest possible match minus one byte, so that any match
        # starting within the carried-over tail of a chunk is guaranteed to be complete once
        # combined with the next one. The chunk size must stay comfortably larger than the
        # overlap so every iteration still reads genuinely new data.
        overlap = max(max_match_len - 1, 0)
        chunk_size = max(DEFAULT_STREAM_CHUNK_SIZE, overlap * 4, overlap + 1)

        return _StreamMatchers(literals=literals, patterns=patterns, overlap=overlap, chunk_size=chunk_size)

    def _file_contains_secret(self, path: str, matchers: _StreamMatchers) -> bool:
        with open(path, 'rb') as f:
            carry = b''

            while True:
                buf = f.read(matchers.chunk_size)
                if not buf:
                    return False

                data = carry + buf
                if matchers.contains_match(data):
                    return True

                carry = data[-matchers.overlap:] if matchers.overlap else b''

    def _cache_key(self, path: str, search_path: str, cache_path: Optional[str]) -> str:
        """
        Where `path` is going to live once the caller is done with it, which is what the
        clean-file cache has to be keyed on.

        That is `path` itself, unless the caller handed us a throwaway copy of a tree to work
        on (`cache_path` is then the original it was copied from). In that case the entry
        belongs to the original: the copy is deleted moments later, so keying on it would both
        leak entries and never answer a later question. `shutil.copy2`/`copytree` preserve size
        and nanosecond mtime, so a stat taken from the copy describes the original just as
        well - and if the original has changed since the copy was taken, the stats no longer
        match and the entry is simply ignored, which is the safe direction.
        """
        if cache_path is None:
            return path

        if path == search_path:
            return cache_path

        return os.path.join(cache_path, os.path.relpath(path, search_path))

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _rewrite_file(self, path: str, matchers: _StreamMatchers) -> Tuple[int, int]:
        """
        Rewrite `path` with every secret redacted, atomically.

        Returns the `(size, mtime_ns)` of the file we wrote, sampled *before* it is moved into
        place. Sampling it afterwards would risk attributing somebody else's concurrent append
        to our own write and caching those never-scanned bytes as clean.
        """
        directory = os.path.dirname(path) or '.'
        original_mode = stat.S_IMODE(os.stat(path).st_mode)

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.hide-secrets-')

        try:
            with os.fdopen(fd, 'wb') as tmp_file, open(path, 'rb') as src_file:
                carry = b''

                while True:
                    buf = src_file.read(matchers.chunk_size)
                    data = matchers.redact(carry + buf)

                    if not buf:
                        # EOF: nothing more will ever arrive to complete a match, flush it all.
                        tmp_file.write(data)
                        break

                    if not matchers.overlap:
                        # No secret can span a chunk boundary, so nothing needs to be held back.
                        tmp_file.write(data)
                        carry = b''
                    elif len(data) > matchers.overlap:
                        tmp_file.write(data[:-matchers.overlap])
                        carry = data[-matchers.overlap:]
                    else:
                        carry = data

                tmp_file.flush()
                os.fsync(tmp_file.fileno())

                # Taken while the file is still ours alone - see the docstring. Neither fsync,
                # close, chmod nor the rename below change the mtime we sample here.
                tmp_stat = os.fstat(tmp_file.fileno())

            os.chmod(tmp_path, original_mode)
            os.replace(tmp_path, path)

        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Durability of the rename needs the directory entry on disk; fsync of the file alone
        # does not give us that. This is what replaces the sed implementation's 'sync' call.
        try:
            self._fsync_directory(directory)
        except OSError as exc:
            self.debug("could not fsync directory '{}': {}".format(directory, exc))

        return tmp_stat.st_size, tmp_stat.st_mtime_ns

    def _scrub_file(self, path: str, matchers: _StreamMatchers, cache_key: Optional[str] = None) -> None:
        key = cache_key if cache_key is not None else path

        stat_result = os.stat(path)
        current = (stat_result.st_size, stat_result.st_mtime_ns)

        if self._clean_files.get(key) == current:
            return

        if not self._file_contains_secret(path, matchers):
            self._clean_files[key] = current
            return

        redacted = self._rewrite_file(path, matchers)

        if key == path:
            # All matches were just replaced with 'hidden', which is not itself one of the
            # secrets, so the file is clean again - cache it, sparing the next call a
            # redundant re-scan.
            self._clean_files[key] = redacted

        else:
            # We redacted a copy of `key`, not `key` itself. The original still has the secret
            # in it, so it must not be remembered as clean.
            self._clean_files.pop(key, None)

    def _hide_secrets_stream(self, search_path: str, cache_path: Optional[str] = None) -> None:
        built_matchers = self._build_stream_matchers()

        # NOTE: We will deprecate this crazy module once TFT-1813
        if built_matchers is None:
            self.debug("No secrets to hide, all secrets had empty values")
            return

        # A plain local (rather than relying on narrowing of `built_matchers`) so the closure
        # below is typed as `_StreamMatchers`, not `Optional[_StreamMatchers]`.
        matchers: _StreamMatchers = built_matchers

        if not os.path.exists(search_path):
            self.debug("search path '{}' does not exist, nothing to hide".format(search_path))
            return

        self.debug("Hiding secrets from all files under '{}' path (stream implementation)".format(search_path))

        def _run() -> Result[bool, bool]:
            try:
                for path in self._iter_regular_files(search_path):
                    self._scrub_file(path, matchers, cache_key=self._cache_key(path, search_path, cache_path))
            except OSError as exc:
                self.warn("Hiding secrets under '{}' failed, retrying: {}".format(search_path, exc), sentry=True)
                return Result.Error(False)

            return Result.Ok(True)

        try:
            gluetool.utils.wait(
                "hiding secrets under '{}'".format(search_path),
                _run,
                timeout=self.option('retry-timeout'),
                tick=self.option('retry-tick')
            )
        except gluetool.GlueError:
            raise gluetool.GlueError('Failed to hide secrets, secrets could be leaked!')

    def _hide_secrets_sed(self, search_path: str) -> None:
        # POSIX.2 Basic Regular Expressions (BREs) have a specific set of characters
        # that you need to escape to use them as literals.
        #
        # Here's a list of special characters in POSIX.2 BREs:
        # * Backslash '\'
        # * Dot '.'
        # * Asterisk '*'
        # * Square brackets '[' and ']'
        # * Caret '^'
        # * Dollar sign '$'
        #
        # Note that backslash needs to be escaped separately
        #
        # We need to also escape:
        # * Pipe '|' - because we use sed with '|' character
        def _posix_bre_escaped(value: str) -> str:
            value = value.replace('\\', '\\\\')
            for escape in r".*[]^$|":
                value = value.replace(escape, r'\{}'.format(escape))
            # Secrets can be indented with spaces, so we need to match them
            value = value.replace('\n', '[[:space:]]*\\n[[:space:]]*')
            return value

        sed_expr = '\n'.join('s|{}|hidden|g'.format(_posix_bre_escaped(value)) for value in self._secrets)

        # NOTE: We will deprecate this crazy module once TFT-1813
        if not sed_expr:
            self.debug("No secrets to hide, all secrets had empty values")
            return

        self.debug("Hiding secrets from all files under '{}' path".format(search_path))

        with tempfile.NamedTemporaryFile(mode='w', dir='.') as temp:
            temp.write(sed_expr)
            temp.flush()
            # -print0 puts a null byte as a separator between each item
            # -0 expects such separator so no more separating by space which takes quotes into account
            command = "find '{}' -type f -print0 | xargs -0 -i##### sed -z -i -f '{}' '#####'".format(
                search_path, temp.name)

            def _run_sed() -> Result[bool, bool]:
                try:
                    output = gluetool.utils.Command([command], logger=self.logger).run(shell=True)
                except gluetool.GlueCommandError as exc:
                    self.warn('sync command "{}" failed, retrying: {}'.format(command, exc), sentry=True)
                    return Result.Error(False)

                output.log(self.logger)
                if output.exit_code != 0:
                    raise gluetool.GlueError('Failed to hide secrets, secrets could be leaked!')

                return Result.Ok(True)

            gluetool.utils.wait(
                "Running '{}'".format(command),
                _run_sed,
                timeout=self.option('retry-timeout'),
                tick=self.option('retry-tick')
            )

        # Be paranoic that modified files were written to the disk
        cmd = ['sync', search_path]

        def _run_sync() -> Result[bool, bool]:
            try:
                gluetool.utils.Command(cmd, logger=self.logger).run()
            except gluetool.GlueCommandError as exc:
                self.warn('sync command "{}" failed, retrying: {}'.format(" ".join(cmd), exc), sentry=True)
                return Result.Error(False)
            return Result.Ok(True)

        try:
            gluetool.utils.wait(
                "sync of '{}'".format(search_path),
                _run_sync,
                timeout=self.option('retry-timeout'),
                tick=self.option('retry-tick')
            )

        except gluetool.GlueError:
            self.warn('Failed to sync modified files to disk.', sentry=True)

    def hide_secrets(self, search_path: Optional[str] = None, cache_path: Optional[str] = None) -> None:
        """
        Redact every known secret from all files under `search_path`, defaulting to the
        `--search-path` option.

        :param cache_path: the path `search_path` was copied from, when it is a throwaway copy
            of a tree. Only affects which paths the clean-file cache is keyed on, so that the
            next copy of the same tree can skip whatever has not changed since. Ignored by the
            'sed' implementation, which keeps no cache.
        """
        search_path = search_path or self.option('search-path')
        assert search_path

        if self.option('implementation') == 'stream':
            self._hide_secrets_stream(search_path, cache_path=cache_path)
        else:
            self._hide_secrets_sed(search_path)

    def destroy(self, failure: Optional[Any] = None) -> None:
        self.hide_secrets()
