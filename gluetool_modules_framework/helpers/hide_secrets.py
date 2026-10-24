# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import heapq
import os
import re
import stat
import tempfile

import gluetool

from dataclasses import dataclass, field
from gluetool.result import Result

from typing import Any, BinaryIO, Iterator, List, Optional, Set, Tuple, Union  # noqa

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

# Whitespace, as `str.isspace()` defines it, which `\s` does not match in a bytes pattern.
# Workers start citool with no locale set, Python coerces that to C.UTF-8 (PEP 538), and sed
# inherits it. There, its '[[:space:]]' matches some of these characters too. Using all of
# them, 'stream' hides at least what 'sed' does, whichever glibc decides what '[[:space:]]' is.
_UNICODE_SPACES = (
    '\x1c\x1d\x1e\x1f\x85\xa0\u1680'
    + ''.join(map(chr, range(0x2000, 0x200b)))
    + '\u2028\u2029\u202f\u205f\u3000'
)

# Whitespace in UTF-8, and whitespace other than '\n': the characters of `\s` and
# `_UNICODE_SPACES`, matched a run of the same kind at a time, which `re` does fast. Every
# multibyte encoding starts with a byte which never continues one, so occurrences of these
# never overlap, and a run of whitespace splits into them in one way only.
_MULTIBYTE_SPACE = br'(?:\xc2[\x85\xa0]|\xe1\x9a\x80|\xe2\x80[\x80-\x8a\xa8\xa9\xaf]|\xe2\x81\x9f|\xe3\x80\x80)++'
_SPACE = br'(?:[\t\n\x0b\x0c\r\x1c-\x1f ]++|' + _MULTIBYTE_SPACE + br')'
_SPACE_NOT_NEWLINE = br'(?:[\t\x0b\x0c\r\x1c-\x1f ]++|' + _MULTIBYTE_SPACE + br')'

_ANY_SPACE = re.compile(_SPACE)

# Every byte which is whitespace, or a part of it: what `\s` matches in a bytes pattern, and
# each byte of the encodings of `_UNICODE_SPACES`. Some characters other than whitespace
# consist of these bytes only, U+3001 IDEOGRAPHIC COMMA for one.
_MAYBE_SPACE = bytes(sorted(set(b' \t\n\r\x0b\x0c' + _UNICODE_SPACES.encode('utf-8'))))
_MAYBE_SPACE_CLASS = b''.join(br'\x%02x' % byte for byte in _MAYBE_SPACE)

# Whitespace at either end of a secret.
_SECRET_EDGE_SPACE = re.compile(br'\A%s+|%s+\Z' % (_SPACE, _SPACE))

# Whitespace inside a multiline secret which contains at least one line break, as a capturing
# group so `split()` keeps it. It matches the whole run of whitespace around the line break,
# leaving the text on either side of it starting and ending with non-whitespace.
_SECRET_LINE_BREAK = re.compile(br'((?:%s*\n)+%s*)' % (_SPACE_NOT_NEWLINE, _SPACE))


def _literal_spans(literal: bytes, data: bytes, limit: int) -> Iterator[Tuple[int, int]]:
    """
    Yield ``(start, end)`` of every run of overlapping occurrences of ``literal`` starting before
    ``limit``, each run merged into one span.

    Of the occurrences starting inside a run, only the last one can extend it, the others end
    inside it already. Jumping straight to that one keeps the work low even for a secret like
    'aaaa' in a file of nothing but 'a'.
    """

    size = len(literal)

    # An occurrence starting before `limit` fits in `data[:limit - 1 + size]`.
    start = data.find(literal, 0, limit - 1 + size)

    while start != -1:
        last, end = start, start + size

        while True:
            last = data.rfind(literal, last + 1, min(end, limit) - 1 + size)

            if last == -1:
                break

            end = last + size

        yield start, end

        start = data.find(literal, end, limit - 1 + size)


@dataclass
class _MultilinePattern:
    """
    A secret spanning multiple lines.

    The secret is split into its pieces of text and the runs of whitespace between them which
    contain a line break. The pattern matches the pieces literally, and each run as any run of
    whitespace with at least as many line breaks, no matter how long, mirroring the
    '[[:space:]]*' padding of the sed implementation.

    Every piece starts and ends with non-whitespace, and each run compiles to N times any
    whitespace but '\\n' followed by '\\n', then any whitespace, which can match a given run of
    whitespace in one way only. Its quantifiers are possessive, so the pattern never backtracks
    into a run, even when the secret contains blank lines.

    Its number of non-whitespace bytes is exact. `_StreamMatchers.boundary` counts at most as
    many in the data, so it never settles a match too early.
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
        self._nonspace_tail = re.compile(
            br'(?:[%s]*+[^%s]){%d}' % (_MAYBE_SPACE_CLASS, _MAYBE_SPACE_CLASS, self.nonspace)
        ) if self.nonspace else None

    def spans(self, data: bytes, limit: int) -> Iterator[Tuple[int, int]]:
        """
        Yield ``(start, end)`` of every run of overlapping matches, of one secret or several,
        starting before ``limit``, each run merged into one span, in order.

        Matches are merged as they are found, so memory use does not grow with their number,
        however densely they overlap.
        """

        found = heapq.merge(
            *[_literal_spans(literal, data, limit) for literal in self.literals],
            *[multiline.spans(data, limit) for multiline in self.patterns]
        )

        run_start = run_end = 0

        for start, end in found:
            if start < run_end:
                run_end = max(run_end, end)
                continue

            if run_end > run_start:
                yield run_start, run_end

            run_start, run_end = start, end

        if run_end > run_start:
            yield run_start, run_end

    def contains(self, data: bytes) -> bool:
        """
        Return whether ``data`` contains a match of any secret, stopping at the first one.
        """

        return any(literal in data for literal in self.literals) or any(
            next(multiline.spans(data, len(data)), None) is not None for multiline in self.patterns
        )

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
        Return the index just past the start of the last `nonspace` bytes of ``data`` which
        cannot be a part of whitespace, or 0 when there are fewer.
        """

        assert self._nonspace_tail is not None

        # Grow a tail of `data` until it holds enough non-whitespace bytes, counting them with
        # `translate()` rather than a regex, which on long whitespace backtracks at a fraction
        # of the speed. Then, since `re` cannot search backwards, match the reversed tail.
        #
        # Every byte which may be a part of whitespace counts as whitespace, including those of
        # a character cut off by either end of the tail. Counting too few non-whitespace bytes
        # only holds back more of `data`, counting too many would settle a pending match.
        size = 4 * self.nonspace

        while True:
            tail = data[-size:]

            if len(tail.translate(None, _MAYBE_SPACE)) >= self.nonspace:
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

    def add_secrets(self, secret: Union[str, List[str]]) -> None:
        if isinstance(secret, list):
            self._secrets.update(secret)
        else:
            self._secrets.add(secret)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(HideSecrets, self).__init__(*args, **kwargs)
        self._secrets: Set[str] = set()

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
            stripped = _SECRET_EDGE_SPACE.sub(b'', encoded)

            if not stripped:
                self.warn('Ignoring a secret which consists of whitespace only')
                continue

            parts = _SECRET_LINE_BREAK.split(stripped)

            if len(parts) == 1:
                literals.append(stripped)
                continue

            pieces, runs = parts[0::2], parts[1::2]

            pattern = re.escape(pieces[0]) + b''.join(
                br'(?:%s*+\n){%d}%s*+' % (_SPACE_NOT_NEWLINE, run.count(b'\n'), _SPACE) + re.escape(piece)
                for run, piece in zip(runs, pieces[1:])
            )

            patterns.append(_MultilinePattern(
                prefilter=max(pieces, key=len),
                first=pieces[0],
                pattern=re.compile(pattern),
                nonspace=len(_ANY_SPACE.sub(b'', stripped))
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
    ) -> Iterator[Tuple[bytes, int]]:
        """
        Read ``f`` and yield ``(window, boundary)``: the next part of the file, and how many of
        its leading bytes are settled.

        Each window starts with the unsettled bytes of the previous one, exactly as read. That
        makes matches independent of where reads happen to split the file: every match starting
        among the settled bytes is complete, and is never found again in a later window.
        """

        carry = b''

        while True:
            buf = f.read(matchers.chunk_size)
            window = carry + buf

            if not buf:
                yield window, len(window)
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

            yield window, boundary

            carry = window[boundary:]

    def _file_contains_secret(self, path: str, matchers: _StreamMatchers) -> bool:
        with open(path, 'rb') as f:
            return any(matchers.contains(window) for window, _ in self._scan(path, f, matchers))

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _rewrite_file(self, path: str, matchers: _StreamMatchers) -> None:
        """
        Rewrite `path` with every secret redacted, atomically.
        """
        directory = os.path.dirname(path) or '.'
        original_mode = stat.S_IMODE(os.stat(path).st_mode)

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.hide-secrets-')

        try:
            with os.fdopen(fd, 'wb') as tmp_file, open(path, 'rb') as src_file:
                # How many leading bytes of the current window are already written out. It runs
                # past the settled bytes only when the last match written out does.
                written = 0

                for window, boundary in self._scan(path, src_file, matchers):
                    for start, end in matchers.spans(window, boundary):
                        if end <= written:
                            continue

                        # Overlapping matches, of the same secret or of different ones, are
                        # redacted together, so no part of either is left visible. `spans()`
                        # merges them within a window, this merges them across windows.
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

    def _scrub_file(self, path: str, matchers: _StreamMatchers) -> None:
        # Files without any secret are never rewritten, so their inode and mtime are preserved.
        if self._file_contains_secret(path, matchers):
            self._rewrite_file(path, matchers)

    def _hide_secrets_stream(self, search_path: str) -> None:
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
                    self._scrub_file(path, matchers)
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

    def hide_secrets(self, search_path: Optional[str] = None) -> None:
        search_path = search_path or self.option('search-path')
        assert search_path

        if self.option('implementation') == 'stream':
            self._hide_secrets_stream(search_path)
        else:
            self._hide_secrets_sed(search_path)

    def destroy(self, failure: Optional[Any] = None) -> None:
        self.hide_secrets()
