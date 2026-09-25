# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import os
import re
import stat
import tempfile

import gluetool

from dataclasses import dataclass, field
from gluetool.result import Result

from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Set, Tuple, Union  # noqa

DEFAULT_RETRY_TIMEOUT = 30
DEFAULT_RETRY_TICK = 10
DEFAULT_IMPLEMENTATION = 'sed'

# Size of the read buffer used by the 'stream' implementation. Actual buffer size used at
# runtime is at least this large, and larger still for very long secrets, see
# `_build_stream_matchers`.
DEFAULT_STREAM_CHUNK_SIZE = 1 * 1024 * 1024

# Upper bound on the number of bytes the 'stream' implementation holds back from one read to
# the next. It holds back about as many bytes as the longest secret has, see
# `_StreamMatchers.boundary`, and only the first line of a secret followed by this much
# whitespace can push it further. Such a file fails, rather than risks a leak.
STREAM_MAX_CARRY = 16 * 1024 * 1024

# What `\s` matches in a bytes pattern, and what `bytes.strip()` strips.
WHITESPACE = b' \t\n\r\x0b\x0c'

# Whitespace inside a multiline secret which contains at least one line break, as a capturing
# group so `split()` keeps it. It matches the whole run of whitespace around the line break,
# leaving the text on either side of it starting and ending with non-whitespace.
_SECRET_LINE_BREAK = re.compile(br'(\s*\n\s*)')


@dataclass
class _MultilinePattern:
    """
    A secret spanning multiple lines.

    The secret is split into its pieces of text and the runs of whitespace between them which
    contain a line break. The pattern matches the pieces literally, and each run as any run of
    whitespace with at least as many line breaks, no matter how long, mirroring the
    '[[:space:]]*' padding of the sed implementation.

    Every piece starts and ends with non-whitespace, and each run compiles to
    '(?:[^\\S\\n]*\\n){N}\\s*', which can match a given run of whitespace in one way only. The
    pattern therefore never backtracks over a run more than once, even when the secret contains
    blank lines.
    """

    # The longest piece. Every match contains it, so when it is missing, the regex does not run.
    prefilter: bytes

    # The first piece. Every match starts with it.
    first: bytes

    pattern: re.Pattern[bytes]

    # Number of non-whitespace bytes in the secret, and therefore in every match of it.
    nonspace: int

    def spans(self, data: bytes, limit: int) -> Iterator[Tuple[int, int]]:
        """
        Yield ``(start, end)`` of every match starting before ``limit``, overlapping ones
        included.
        """

        if self.prefilter not in data:
            return

        pos = 0

        while True:
            match = self.pattern.search(data, pos)

            if match is None or match.start() >= limit:
                return

            yield match.span()

            pos = match.start() + 1


@dataclass
class _StreamMatchers:
    """
    Compiled matchers used by the 'stream' implementation, plus what they need to find secrets
    split across read buffers.
    """

    # Secrets with no newline, matched as plain bytes (no regex involved).
    literals: List[bytes] = field(default_factory=list)

    # Secrets spanning multiple lines.
    patterns: List[_MultilinePattern] = field(default_factory=list)

    # Number of bytes read from the file per iteration.
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE

    # Length of the longest literal, and of the longest first piece of a multiline secret,
    # minus one byte: a match of either which starts closer than this to the end of a buffer
    # may continue in the next one.
    literal_overlap: int = field(init=False)
    first_overlap: int = field(init=False)

    # The most non-whitespace bytes in any multiline secret.
    nonspace: int = field(init=False)

    _nonspace_tail: Optional[re.Pattern[bytes]] = field(init=False)

    def __post_init__(self) -> None:
        self.literal_overlap = max((len(literal) for literal in self.literals), default=1) - 1
        self.first_overlap = max((len(multiline.first) for multiline in self.patterns), default=1) - 1
        self.nonspace = max((multiline.nonspace for multiline in self.patterns), default=0)

        # Matched against a buffer reversed, see `boundary`.
        self._nonspace_tail = re.compile(br'(?:\s*\S){%d}' % self.nonspace) if self.nonspace else None

    def spans(self, data: bytes, limit: int) -> List[Tuple[int, int]]:
        """
        Return ``(start, end)`` of every match of every secret starting before ``limit``,
        overlapping ones included, sorted.
        """

        found: List[Tuple[int, int]] = []

        for literal in self.literals:
            pos = data.find(literal)

            while 0 <= pos < limit:
                found.append((pos, pos + len(literal)))
                pos = data.find(literal, pos + 1)

        for multiline in self.patterns:
            found.extend(multiline.spans(data, limit))

        found.sort()

        return found

    def boundary(self, data: bytes) -> int:
        """
        Return how many leading bytes of ``data`` are settled: every match starting among them
        ends within ``data``, so nothing read later can change how they are redacted.

        A literal match is settled when ``data`` holds all of its bytes. A multiline match
        starts with the first piece of its secret, so it can be pending only where that piece
        occurs. It also ends with non-whitespace and holds exactly as many non-whitespace bytes
        as its secret, however much whitespace separates its lines - so it is settled when the
        rest of ``data`` holds at least that many non-whitespace bytes.
        """

        # A match of a literal, or of the first piece of a multiline secret, may be cut off by
        # the end of `data`.
        boundary = len(data) - max(self.literal_overlap, self.first_overlap)

        if any(multiline.first in data for multiline in self.patterns):
            nonspace_boundary = self._nonspace_boundary(data)

            for multiline in self.patterns:
                pending = data.find(multiline.first, nonspace_boundary)

                if pending != -1:
                    boundary = min(boundary, pending)

        return max(boundary, 0)

    def _nonspace_boundary(self, data: bytes) -> int:
        """
        Return the index just past the start of the last `nonspace` non-whitespace bytes of
        ``data``, or 0 when there are fewer.
        """

        assert self._nonspace_tail is not None

        # Grow a tail of `data` until it holds enough non-whitespace bytes, counting them with
        # `translate()` rather than a regex, which on long whitespace backtracks at a fraction
        # of the speed. Then, since `re` cannot search backwards, match the reversed tail.
        size = 4 * self.nonspace

        while True:
            tail = data[-size:]

            if len(tail.translate(None, WHITESPACE)) >= self.nonspace:
                match = self._nonspace_tail.match(tail[::-1])
                assert match is not None

                return len(data) - match.end() + 1

            if size >= len(data):
                return 0

            size *= 4


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
                implementation which redacts files in bounded memory with no size limit.
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

        for secret in self._secrets:
            # Matches the existing sed-based behaviour: an empty secret is not something we
            # can meaningfully redact (and, unlike a targeted literal, would match everywhere).
            if not secret:
                continue

            encoded = secret.encode('utf-8', 'surrogateescape')

            if b'\n' not in encoded:
                literals.append(encoded)
                continue

            # Whitespace around a multiline secret is not secret, and requiring it, as the sed
            # implementation does, only makes the secret harder to find. Without it, every piece
            # starts and ends with non-whitespace, see `_MultilinePattern`.
            stripped = encoded.strip()

            if not stripped:
                self.warn('Ignoring a secret which consists of whitespace only')
                continue

            parts = _SECRET_LINE_BREAK.split(stripped)

            if len(parts) == 1:
                literals.append(stripped)
                continue

            pieces, runs = parts[0::2], parts[1::2]

            pattern = re.escape(pieces[0]) + b''.join(
                br'(?:[^\S\n]*\n){%d}\s*' % run.count(b'\n') + re.escape(piece)
                for run, piece in zip(runs, pieces[1:])
            )

            patterns.append(_MultilinePattern(
                prefilter=max(pieces, key=len),
                first=pieces[0],
                pattern=re.compile(pattern),
                nonspace=len(stripped.translate(None, WHITESPACE))
            ))

        if not literals and not patterns:
            return None

        matchers = _StreamMatchers(literals=literals, patterns=patterns)

        # Each read should bring in plenty of new data compared to what is held back from the
        # previous one, which for ordinary text is about as long as the longest secret.
        matchers.chunk_size = max(DEFAULT_STREAM_CHUNK_SIZE, 4 * matchers.literal_overlap, 4 * matchers.nonspace)

        return matchers

    def _scan(
        self,
        path: str,
        f: BinaryIO,
        matchers: _StreamMatchers
    ) -> Iterator[Tuple[bytes, int, List[Tuple[int, int]]]]:
        """
        Read ``f`` and yield ``(window, boundary, spans)``: the next part of the file, how many
        of its leading bytes are settled, and the matches starting among those bytes.

        Each window starts with the unsettled bytes of the previous one, exactly as read. That
        makes matches independent of where reads happen to split the file: every match starting
        among the settled bytes is complete, and is never found again in a later window.
        """

        carry = b''

        while True:
            buf = f.read(matchers.chunk_size)
            window = carry + buf

            if not buf:
                yield window, len(window), matchers.spans(window, len(window))
                return

            boundary = matchers.boundary(window)

            # The first line of a secret, followed by more whitespace than we are willing to
            # hold in memory before its other lines show up, if they ever do.
            if len(window) - boundary > STREAM_MAX_CARRY:
                raise gluetool.GlueError(
                    "Cannot hide secrets in '{}', {} bytes of whitespace may split a secret".format(
                        path, len(window) - boundary
                    )
                )

            yield window, boundary, matchers.spans(window, boundary)

            carry = window[boundary:]

    def _file_contains_secret(self, path: str, matchers: _StreamMatchers) -> bool:
        with open(path, 'rb') as f:
            return any(spans for _, _, spans in self._scan(path, f, matchers))

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
                # How many leading bytes of the current window are already written out. It runs
                # past the settled bytes only when the last match written out does.
                written = 0

                for window, boundary, spans in self._scan(path, src_file, matchers):
                    for start, end in spans:
                        if end <= written:
                            continue

                        # Overlapping matches, of the same secret or of different ones, are
                        # redacted together, so no part of either is left visible.
                        if start < written:
                            written = end
                            continue

                        tmp_file.write(window[written:start])
                        tmp_file.write(b'hidden')
                        written = end

                    if written < boundary:
                        tmp_file.write(window[written:boundary])
                        written = boundary

                    written -= boundary

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
