# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import codecs
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, urlsplit

import gluetool
from gluetool.result import Result
from gluetool.utils import requests, wait

from gluetool_modules_framework.libs.artifacts import artifacts_location
from gluetool_modules_framework.libs.guest_setup import STAGES_ORDERED
from gluetool_modules_framework.libs.results import AIAnalysis, TestCase, TestSuite
from gluetool_modules_framework.libs.test_schedule import TestScheduleEntryState, TestScheduleResult
from gluetool_modules_framework.libs.test_schedule_tmt import TMT_LOG
from gluetool_modules_framework.provision.artemis import ArtemisGuest

# Type annotations
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, cast  # noqa

if TYPE_CHECKING:
    from requests.models import Response  # noqa
    from gluetool_modules_framework.testing.test_schedule_tmt import TestScheduleEntry  # noqa

#: Log Detective imposes a filename character-set restriction.
_NAME_SAFE_RE = re.compile(r'[^a-zA-Z0-9._\-/ ]')

# Default number of log artifacts selected for each analysis
DEFAULT_ARTIFACT_LIMIT = 15

# Maximum artifact name length enforced by Log Detective API
MAX_ARTIFACT_NAME_LENGTH = 255

DEFAULT_RETRY_TIMEOUT = 120
DEFAULT_RETRY_TICK = 10
DEFAULT_PLAN_TIMEOUT = 180
ARTIFACT_WAIT_TIMEOUT = 30
ARTIFACT_CHECK_TIMEOUT = 3
ARTIFACT_CHECK_TICK = 5
TEXT_SAMPLE_SIZE = 4096


def _retry_after_seconds(value: Optional[str], default: int) -> float:
    """Parse a Retry-After header as seconds, accepting both supported HTTP forms."""
    if not value:
        return float(default)

    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return float(default)

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _sanitize_name(name: str) -> str:
    """Replace characters not allowed in Log Detective artifact names with underscores."""
    return _NAME_SAFE_RE.sub('_', name)


def _truncate_name(name: str) -> str:
    """Truncate *name* to 255 characters, preserving a file extension when possible."""
    if len(name) <= MAX_ARTIFACT_NAME_LENGTH:
        return name
    stem, _, ext = name.rpartition('.')
    if not stem:
        # No extension; truncate the whole thing.
        return name[:MAX_ARTIFACT_NAME_LENGTH]
    ext = '.' + ext
    if len(ext) >= MAX_ARTIFACT_NAME_LENGTH:
        # There is not enough room to preserve both the stem and this extension.
        return name[:MAX_ARTIFACT_NAME_LENGTH]
    return stem[:MAX_ARTIFACT_NAME_LENGTH - len(ext)] + ext


def _make_unique_name(raw: str, seen_names: Dict[str, int]) -> str:
    """Return a unique, sanitized artifact name within a files list, capped at 255 chars."""
    safe = _sanitize_name(raw)
    count = 0

    while True:
        suffix = '_{}'.format(count) if count else ''
        if not suffix:
            name = _truncate_name(safe)
        else:
            stem, separator, extension = safe.rpartition('.')
            if not stem:
                # Treat names without a usable extension (including dotfiles) as a single stem.
                name = safe[:MAX_ARTIFACT_NAME_LENGTH - len(suffix)] + suffix
            else:
                extension = separator + extension
                available = MAX_ARTIFACT_NAME_LENGTH - len(extension) - len(suffix)
                if available <= 0:
                    # An unusually long extension leaves no room for both stem and suffix.
                    name = safe[:MAX_ARTIFACT_NAME_LENGTH - len(suffix)] + suffix
                else:
                    name = stem[:available] + suffix + extension

        if name not in seen_names:
            seen_names[name] = 1
            return name
        count += 1


def _is_text_file(path: str) -> bool:
    """Check a small UTF-8 sample before sending an auxiliary artifact as a log."""
    try:
        with open(path, 'rb') as stream:
            sample = stream.read(TEXT_SAMPLE_SIZE)
    except OSError:
        return False

    if b'\0' in sample:
        return False
    try:
        # An incremental decoder accepts a multibyte character cut off at the sample boundary.
        codecs.getincrementaldecoder('utf-8')().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


class LogDetective(gluetool.Module):
    """
    Sends tmt test logs to Log Detective ``/analyze`` and stores the analysis, parsed into a
    :py:class:`~gluetool_modules_framework.libs.results.AIAnalysis` structure, in ``<ai_summary>``.

    Each failing testcase is analyzed **individually** using its own logs, so its ``ai_summary``
    reflects that specific test's failure rather than a shared plan-level summary (tests in a
    single plan routinely fail for unrelated reasons). Plans that error before producing any
    testcases fall back to a single plan-level analysis stored on the suite.

    The module overloads ``serialize_test_schedule_entry_results`` so it runs as part of the
    normal result-serialization chain, after the tmt runner has populated the test suite.
    Plans with a failed/error result or an error state are analyzed;
    passed/skipped/pending plans are skipped.
    When archive is loaded, its destroy-stage artifacts are published before URLs are checked.
    Analysis runs during the report's destroy pass, after archive publication. Each plan has
    its own time budget.
    """

    name = 'log-detective'
    description = 'Sends test logs to the Log Detective /analyze API and stores AI analysis in results.'

    # In dry-run mode the module still serializes results but must not POST to external services.
    supported_dryrun_level = gluetool.glue.DryRunLevels.DRY

    options = {
        'base-url': {
            'help': 'Base URL of the Log Detective server (e.g. http://logdetective.example.com:8080).',
            'metavar': 'URL',
        },
        'api-token': {
            'help': 'Bearer token for Log Detective authentication. Optional; sent only when set.',
            'default': None,
            'metavar': 'TOKEN',
        },
        'artifact-limit': {
            'help': 'Maximum number of log artifacts per analysis (default: %(default)s).',
            'type': int,
            'default': DEFAULT_ARTIFACT_LIMIT,
        },
        'retry-timeout': {
            'help': 'Per-task timeout for submitting and polling Log Detective in seconds, subject to '
                    'the plan-timeout budget. '
                    '(default: %(default)s)',
            'type': int,
            'default': DEFAULT_RETRY_TIMEOUT,
        },
        'retry-tick': {
            'help': 'Retry interval for transient errors or when Retry-After is missing, in seconds. '
                    '(default: %(default)s)',
            'type': int,
            'default': DEFAULT_RETRY_TICK,
        },
        'plan-timeout': {
            'help': 'Maximum time spent analyzing one plan, including artifact availability '
                    'checks (default: %(default)s seconds).',
            'type': int,
            'default': DEFAULT_PLAN_TIMEOUT,
        },
    }

    required_options = ('base-url',)

    shared_functions = ['serialize_test_schedule_entry_results', 'prepare_final_results']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(LogDetective, self).__init__(*args, **kwargs)
        # The report module rebuilds suites on each pass, so use stable schedule/test identities.
        # Record selected and submitted URLs for successful analyses. A result is final only when
        # every selected URL was submitted; a partial result can improve on a later final pass.
        self._analysis_cache: Dict[Any, AIAnalysis] = {}
        self._analysis_sources: Dict[Any, Tuple[Tuple[Tuple[str, str], ...],
                                                Tuple[Tuple[str, str], ...]]] = {}
        self._complete_analyses: Set[Any] = set()
        # An exhausted transport retry budget indicates an unavailable endpoint.
        self._endpoint_unavailable = False
        self._plan_deadlines: Dict[Any, float] = {}
        self._active_plan: Optional[Any] = None
        self._budget_exhausted_warned: Set[Any] = set()
        self._registered_token: Optional[str] = None

    def sanity(self) -> None:
        for option in ('retry-timeout', 'retry-tick', 'plan-timeout'):
            if self.option(option) is not None and self.option(option) <= 0:
                raise gluetool.GlueError('{} must be positive'.format(option))
        if self._artifact_limit() <= 0:
            raise gluetool.GlueError('artifact-limit must be positive')

    def execute(self) -> None:
        """Register the token once hide-secrets has reached its execute stage."""
        token = self.option('api-token')
        if not token:
            return
        if not self.has_shared('add_secrets'):
            self.warn('Log Detective api-token requires the hide-secrets add_secrets function.')
            return
        try:
            self.shared('add_secrets', token)
        except Exception:
            # The exception may contain the unregistered token, so keep it out of logs.
            self.warn('Could not register Log Detective api-token for secret hiding.', sentry=True)
            return
        self._registered_token = token

    def _artifact_limit(self) -> int:
        """Return the configured artifact count, including its default for direct module use."""
        configured = self.option('artifact-limit')
        # The option declares type=int, but gluetool.Module.option is typed as Any.
        return DEFAULT_ARTIFACT_LIMIT if configured is None else cast(int, configured)

    def _remaining_budget(self) -> float:
        """Return the active plan's remaining analysis time."""
        if self._active_plan is None:
            # Direct calls outside result serialization have no plan to charge.
            return float(self.option('plan-timeout') or DEFAULT_PLAN_TIMEOUT)
        remaining = max(0.0, self._plan_deadlines[self._active_plan] - time.monotonic())
        if remaining <= 0 and self._active_plan not in self._budget_exhausted_warned:
            self._budget_exhausted_warned.add(self._active_plan)
            self.warn('Log Detective analysis time exhausted for plan {}; omitting remaining AI summaries.'.format(
                self._active_plan.plan), sentry=True)
        return remaining

    def _ready_files(self, files: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Return inline files and URL artifacts reachable from this pipeline worker.

        Inline ``content`` entries need no availability check; if there are no eligible URL
        entries, return them immediately. URL entries must use HTTP(S)
        without parameters, a query, or a fragment. For each eligible URL, try HEAD and accept
        HTTP 200; if HEAD returns 405 or 501, try a streamed, one-byte-range GET and accept
        HTTP 200 or 206. Failed checks and request exceptions leave that URL out of the result.

        Each pass attempts the URLs not yet ready. Polling stops when a URL named
        ``testout.log`` is ready, or the first URL entry if none has that name. It also stops after
        at most 30 seconds or when the plan's analysis budget expires. Thus a ready primary
        can end the wait while other URLs are still unavailable. Return only ready entries,
        in their original order; callers may submit a partial set or skip analysis if it is
        empty. A successful check establishes reachability from this worker at check time,
        not that the Log Detective server can fetch the URL later.
        """
        ready: Dict[str, Dict[str, str]] = {}
        remote: List[Dict[str, str]] = []

        for entry in files:
            if 'content' in entry:
                ready[entry['name']] = entry
                continue

            url = entry.get('url', '')
            if not isinstance(url, str):
                self.warn('Skipping Log Detective artifact with an unsupported URL: {}'.format(entry['name']))
                continue
            try:
                parsed = urlparse(url)
                valid = parsed.scheme in ('http', 'https') and bool(parsed.netloc) and not (
                    parsed.params or parsed.query or parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                self.warn('Skipping Log Detective artifact with an unsupported URL: {}'.format(entry['name']))
                continue
            remote.append(entry)

        if not remote:
            return [entry for entry in files if entry['name'] in ready]

        # Prefer the actual testcase output to plan-level context when deciding whether to wait.
        primary = next((entry['name'] for entry in remote if entry['name'] == 'testout.log'),
                       remote[0]['name'])
        availability_deadline = time.monotonic() + min(ARTIFACT_WAIT_TIMEOUT, self._remaining_budget())

        while time.monotonic() < availability_deadline:
            for entry in remote:
                if entry['name'] in ready:
                    continue
                remaining = min(availability_deadline - time.monotonic(), self._remaining_budget())
                if remaining <= 0:
                    break
                try:
                    with requests(logger=self.logger) as R:
                        # gluetool wraps head/get and logs response.text. request() leaves a
                        # streamed body unread, including when the server ignores Range.
                        response = R.request('HEAD', entry['url'], stream=True, allow_redirects=True,
                                             timeout=min(ARTIFACT_CHECK_TIMEOUT, remaining))
                        try:
                            head_status = response.status_code
                        finally:
                            response.close()
                        if head_status in (405, 501):
                            # Some artifact stores support GET but not HEAD. A streamed
                            # range request checks reachability without downloading the log.
                            remaining = min(availability_deadline - time.monotonic(), self._remaining_budget())
                            if remaining <= 0:
                                break
                            response = R.request('GET', entry['url'], headers={'Range': 'bytes=0-0'},
                                                 stream=True, allow_redirects=True,
                                                 timeout=min(ARTIFACT_CHECK_TIMEOUT, remaining))
                            try:
                                available = response.status_code in (200, 206)
                            finally:
                                response.close()
                        else:
                            available = head_status == 200
                    if available:
                        ready[entry['name']] = entry
                except Exception as exc:
                    self.debug('Could not check Log Detective artifact {}: {}'.format(entry['name'], exc))

            if primary in ready:
                break
            remaining = min(availability_deadline - time.monotonic(), self._remaining_budget())
            if remaining <= 0:
                break
            time.sleep(min(ARTIFACT_CHECK_TICK, remaining))

        if primary not in ready and time.monotonic() >= availability_deadline and self._remaining_budget() > 0:
            self.warn('Log Detective artifact availability timed out for {} after {} seconds.'.format(
                primary, ARTIFACT_WAIT_TIMEOUT), sentry=True)

        return [entry for entry in files if entry['name'] in ready]

    def _local_file_entry(self, path: str, name: str) -> Dict[str, str]:
        """Reference a complete local log by its published artifact URL."""
        return {'name': name, 'url': artifacts_location(self, path, logger=self.logger)}

    def _build_files_for_testcase(
            self,
            schedule_entry: 'TestScheduleEntry',
            test_case: TestCase,
    ) -> List[Dict[str, str]]:
        """
        Build the ``files`` list for a single failing testcase.

        Includes the shared ``tmt-run.log`` (plan-level execution context) and the testcase's
        regular-file artifacts. The output and journal take precedence when the configured
        limit is small. Source paths let us exclude directory links before sending published
        URLs to Log Detective.

        :param schedule_entry: The tmt schedule entry the testcase belongs to.
        :param test_case: The failing testcase to gather logs for.
        :returns: Named content or URL artifacts suitable for the Log Detective request.
        :rtype: list
        """
        files: List[Dict[str, str]] = []
        seen_names: Dict[str, int] = {}  # name → use-count for de-duplication
        artifact_limit = self._artifact_limit()
        candidate_limit = max(artifact_limit, 3)
        priority = {'testout.log': 0, 'journal.txt': 1, TMT_LOG: 2}

        # tmt-run.log gives the model plan-level execution context for this test.
        tmt_log_path = os.path.join(schedule_entry.work_dirpath, TMT_LOG) if schedule_entry.work_dirpath else None
        if tmt_log_path and os.path.isfile(tmt_log_path):
            files.append(self._local_file_entry(tmt_log_path, _make_unique_name(TMT_LOG, seen_names)))
        else:
            tmt_log_path = None

        # The source artifacts retain local paths, allowing directory links in the public results
        # to be excluded before submitting logs to the analysis API.
        guest_name = test_case.guest.name if test_case.guest else None
        matching_results = [task for task in (schedule_entry.results or [])
                            if task.name == test_case.name and task.serial_number == test_case.serial_number
                            and (task.guest.name if task.guest else None) == guest_name]
        if matching_results:
            # The source list is path-sorted. Keep the main output and journal ahead of
            # auxiliary files so the configured cap cannot crowd them out.
            artifacts = sorted(matching_results[0].artifacts,
                               key=lambda artifact: priority.get(artifact.name, 2))
            for artifact in artifacts:
                if len(files) >= candidate_limit:
                    break
                if not os.path.isfile(artifact.path):
                    continue
                # RESULTS_MISSING exposes tmt-run.log as the testcase's testout.log.
                # It is already present above as the plan log, so do not submit it twice.
                if tmt_log_path and os.path.realpath(artifact.path) == os.path.realpath(tmt_log_path):
                    continue
                if artifact.name.startswith('data/') and not _is_text_file(artifact.path):
                    continue
                files.append(self._local_file_entry(
                    artifact.path, _make_unique_name(artifact.name, seen_names)))
        elif not schedule_entry.results:
            # Support suites built without tmt source artifacts, while excluding its known directory links.
            for log in sorted(test_case.logs, key=lambda entry: priority.get(entry.name, 3)):
                if len(files) >= candidate_limit:
                    break
                if log.name in ('log_dir', 'data'):
                    continue
                files.append({'name': _make_unique_name(log.name, seen_names), 'url': log.href})

        if len(files) > artifact_limit:
            # Keep testcase output and journal before plan context when a low limit cannot
            # accommodate all three. Restore the usual tmt-first order for those selected.
            ranked = sorted(enumerate(files), key=lambda item: (priority.get(item[1]['name'], 3), item[0]))
            files = [entry for _, entry in sorted(ranked[:artifact_limit])]

        return files

    def _build_files_for_suite_fallback(
            self,
            schedule_entry: 'TestScheduleEntry',
    ) -> List[Dict[str, str]]:
        """
        Build the ``files`` list for a plan that errored before producing any testcases.

        Gathers suite-level logs: the failing guest-setup phase log (if any), guest event log,
        Artemis console logs, ``tmt-run.log`` (if it happens to exist), and a synthetic
        ``error.txt`` synthesized from the schedule entry's error state.

        :param schedule_entry: The tmt schedule entry whose logs to collect.
        :returns: Named content or URL artifacts suitable for the Log Detective request.
        :rtype: list
        """
        files: List[Dict[str, str]] = []
        seen_names: Dict[str, int] = {}
        artifact_limit = self._artifact_limit()

        def _try_add(path: str, label: str) -> None:
            if len(files) >= artifact_limit:
                return
            if not os.path.isfile(path):
                return
            files.append(self._local_file_entry(path, _make_unique_name(label, seen_names)))

        # Prioritize the log of the specific failing guest-setup stage (if any).
        if schedule_entry.error_guest_setup_stage:
            failing_stage = schedule_entry.error_guest_setup_stage
            outputs = schedule_entry.guest_setup_outputs.get(failing_stage, [])
            for output in outputs:
                _try_add(output.log_path, output.label)
        else:
            # No specific failing stage; include all guest-setup stage logs in order.
            for stage in STAGES_ORDERED:
                for output in schedule_entry.guest_setup_outputs.get(stage, []):
                    _try_add(output.log_path, output.label)

        # Guest event log (Artemis provisioning events).
        if isinstance(schedule_entry.guest, ArtemisGuest) and schedule_entry.guest.event_log_path:
            _try_add(schedule_entry.guest.event_log_path, 'guest-event.log')

        # Artemis guest logs (console, etc.).
        if isinstance(schedule_entry.guest, ArtemisGuest) and schedule_entry.guest.guest_logs:
            if not schedule_entry.work_dirpath:
                self.warn('No work directory for plan {}; skipping guest console logs.'.format(
                    schedule_entry.plan))
            else:
                for log in schedule_entry.guest.guest_logs:
                    if not log.content and not log.save_empty:
                        continue
                    log_path = os.path.join(
                        schedule_entry.work_dirpath,
                        log.filename.format(guestname=schedule_entry.guest.artemis_id)
                    )
                    _try_add(log_path, log.name)

        # tmt-run.log if it happened to be written before the crash.
        if schedule_entry.work_dirpath:
            _try_add(os.path.join(schedule_entry.work_dirpath, TMT_LOG), TMT_LOG)

        # Synthetic error description when there are no local log candidates.
        if len(files) == 0:
            error_file = self._synthetic_error_file(schedule_entry)
            if error_file:
                files.append(error_file)

        return files

    def _synthetic_error_file(self, schedule_entry: 'TestScheduleEntry') -> Optional[Dict[str, str]]:
        """Describe a plan failure when no published log URL can be submitted."""
        parts = []
        if schedule_entry.error_stage:
            parts.append('Failed stage: {}'.format(schedule_entry.error_stage.value))
        if schedule_entry.error_guest_setup_stage:
            parts.append('Failed guest-setup stage: {}'.format(schedule_entry.error_guest_setup_stage.value))
        if schedule_entry.error_message:
            parts.append('Error message: {}'.format(schedule_entry.error_message))
        if not parts:
            self.warn('No logs or error metadata available for plan: {}'.format(schedule_entry.plan))
            return None
        # _call_logdetective redacts inline content before submitting it.
        return {'name': 'error.txt', 'content': '\n'.join(parts) + '\n'}

    def _build_metadata(
            self,
            schedule_entry: 'TestScheduleEntry',
            test_case: Optional[TestCase] = None,
    ) -> Dict[str, Any]:
        """
        Build the ``build_metadata`` dict to help Log Detective contextualize the failure.

        Populates ``commentary`` with concise run context (artifact under test, plan name,
        the specific test when analyzing a single testcase, compose/arch) and ``infra_status``
        to distinguish infrastructure failures from test failures.

        :param schedule_entry: The tmt schedule entry being analyzed.
        :param test_case: The specific testcase being analyzed, if any; its name is added to the
            commentary so the model knows which test the logs belong to.
        :returns: Dict with ``commentary`` and ``infra_status`` keys, each a string or ``None``.
        :rtype: dict
        """
        commentary_parts: List[str] = []

        # Artifact under test (NVR, Koji task ID, …).
        try:
            primary_task = self.shared('primary_task')
            if primary_task:
                commentary_parts.append('Artifact: {}'.format(str(primary_task)))
        except Exception as exc:
            self.debug('Could not retrieve primary_task for Log Detective commentary: {}'.format(exc))

        if schedule_entry.plan:
            commentary_parts.append('Plan: {}'.format(schedule_entry.plan))

        if test_case is not None and test_case.name:
            commentary_parts.append('Test: {}'.format(test_case.name))

        if schedule_entry.testing_environment:
            env = schedule_entry.testing_environment
            if env.compose:
                commentary_parts.append('Compose: {}'.format(env.compose))
            if env.arch:
                commentary_parts.append('Arch: {}'.format(env.arch))

        commentary = '; '.join(commentary_parts) if commentary_parts else None

        # infra_status lets the model distinguish between infrastructure failures and test failures.
        if schedule_entry.state == TestScheduleEntryState.ERROR:
            parts: List[str] = ['Infrastructure or pipeline error before tests ran.']
            if schedule_entry.error_stage:
                parts.append('Failed at stage: {}.'.format(schedule_entry.error_stage.value))
            if schedule_entry.error_guest_setup_stage:
                parts.append('Failed guest-setup stage: {}.'.format(
                    schedule_entry.error_guest_setup_stage.value))
            if schedule_entry.error_message:
                parts.append('Error: {}'.format(schedule_entry.error_message))
            infra_status = ' '.join(parts)
        else:
            infra_status = 'Infrastructure provisioned and guest set up successfully; tmt executed.'

        return {
            'commentary': commentary,
            'infra_status': infra_status,
        }

    def _call_logdetective(self, files: List[Dict[str, str]], metadata: Dict[str, Any]) -> Optional[AIAnalysis]:
        """
        Submit logs to Log Detective ``/analyze``, poll the resulting task, and return its analysis.

        The API accepts submissions asynchronously. This method follows the ``Location`` header
        until the task is terminal, then parses its ``result`` into an
        :py:class:`~gluetool_modules_framework.libs.results.AIAnalysis`.

        Best-effort: swallows all exceptions and returns ``None`` on failure so that AI analysis
        never disrupts the pipeline.

        :param list files: List of named URL or content artifacts to send for analysis.
        :param dict metadata: ``build_metadata`` dict (``commentary``, ``infra_status``).
        :returns: The parsed :py:class:`AIAnalysis`, or ``None`` on error or missing explanation.
        :rtype: AIAnalysis or None
        """
        if self.dryrun_enabled:
            self.debug('Dry-run mode: skipping Log Detective API call.')
            return None

        if self._endpoint_unavailable:
            self.debug('Log Detective endpoint marked unavailable earlier this run; skipping call.')
            return None

        if self._remaining_budget() <= 0:
            return None

        if self.option('api-token') and self.option('api-token') != self._registered_token:
            self.warn('Log Detective api-token was not registered with hide-secrets; skipping analysis.')
            return None

        # Request JSON can be logged by the HTTP wrapper. Fail closed if the in-memory
        # redactor is unavailable, including for synthetic error text and metadata.
        if not self.has_shared('redact_text'):
            self.warn('Log Detective analysis requires the hide-secrets redact_text function.')
            return None

        try:
            safe_files: List[Dict[str, str]] = []
            safe_names: Dict[str, int] = {}
            for entry in files:
                safe_name = _make_unique_name(self.shared('redact_text', entry['name']), safe_names)
                if 'content' in entry:
                    safe_files.append({'name': safe_name,
                                       'content': self.shared('redact_text', entry['content'])})
                else:
                    # A secret in a URL cannot be replaced without breaking its location.
                    if self.shared('redact_text', entry['url']) != entry['url']:
                        self.warn('Skipping Log Detective URL artifact whose URL contains a registered secret.')
                        continue
                    safe_files.append({'name': safe_name, 'url': entry['url']})
            safe_metadata = {key: self.shared('redact_text', value) if isinstance(value, str) else value
                             for key, value in metadata.items()}
        except Exception:
            self.warn('Could not redact Log Detective analysis request.', sentry=True)
            return None
        if not safe_files:
            return None

        base_url = self.option('base-url').rstrip('/')
        submit_url = '{}/analyze'.format(base_url)

        headers: Dict[str, str] = {'Content-Type': 'application/json'}
        token = self.option('api-token')
        if token:
            headers['Authorization'] = 'Bearer {}'.format(token)

        retry_timeout = self.option('retry-timeout')
        if retry_timeout is None:
            retry_timeout = DEFAULT_RETRY_TIMEOUT

        retry_tick = self.option('retry-tick')
        if retry_tick is None:
            retry_tick = DEFAULT_RETRY_TICK

        body: Dict[str, Any] = {
            # Reusing this ID makes submission retries safe if the server accepted the request
            # before the connection failed.
            'id': str(uuid.uuid4()),
            'files': safe_files,
            'build_metadata': safe_metadata,
        }

        plan_remaining = self._remaining_budget()
        plan_limited = plan_remaining <= retry_timeout
        deadline = time.monotonic() + min(retry_timeout, plan_remaining)

        def _request_with_retry(
                method: str,
                url: str,
                json_body: Optional[Dict[str, Any]] = None
        ) -> 'Response':
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Log Detective request deadline expired')

            tick = max(1, min(retry_tick, int(remaining)))

            def _retry_or_stop() -> Result[Optional['Response'], bool]:
                # gluetool.wait sleeps for a whole tick after an error. Stop it before that
                # sleep could take this request beyond the shared analysis deadline.
                return Result.Ok(None) if deadline - time.monotonic() <= tick else Result.Error(False)

            def _do_request() -> Result[Optional['Response'], bool]:
                request_remaining = deadline - time.monotonic()
                if request_remaining <= 0:
                    return Result.Ok(None)

                with requests(logger=self.logger) as R:
                    try:
                        resp = R.request(
                            method,
                            url,
                            json=json_body,
                            headers=headers,
                            timeout=request_remaining,
                        )
                    except Exception as exc:
                        self.warn('Log Detective {} request failed: {}'.format(method, exc))
                        return _retry_or_stop()

                    # Rate limits and server errors may clear on retry. Other HTTP errors are
                    # permanent and should not cause duplicate submissions or polling loops.
                    if resp.status_code == 429 or resp.status_code >= 500:
                        self.warn('Log Detective returned HTTP {} (transient), will retry: {}'.format(
                            resp.status_code, resp.text[:200]))
                        return _retry_or_stop()
                    if not resp.ok:
                        self.warn('Log Detective returned HTTP {} (permanent): {}'.format(
                            resp.status_code, resp.text[:200]))
                    return Result.Ok(resp)

            response = wait(
                'Log Detective {} {}'.format(method, url),
                _do_request,
                timeout=remaining,
                tick=tick,
            )
            if response is None:
                raise TimeoutError('Log Detective request deadline expired during retries')
            return response

        def _decode_json(response: 'Response') -> Optional[Dict[str, Any]]:
            try:
                data = response.json()
            except Exception as exc:
                self.warn('Log Detective returned non-JSON response: {}'.format(exc))
                return None
            if not isinstance(data, dict):
                self.warn('Log Detective returned a JSON response that is not an object.')
                return None
            return data

        def _analysis_from_task(task: Dict[str, Any]) -> Optional[AIAnalysis]:
            status = task.get('status')
            if status == 'error':
                self.warn('Log Detective analysis task failed: {}'.format(task.get('error')))
                return None
            if status == 'cancelled':
                self.warn('Log Detective analysis task was cancelled.')
                return None
            if status != 'done':
                return None

            result = task.get('result')
            if not isinstance(result, dict):
                self.warn('Log Detective completed without an analysis result.')
                return None
            analysis = AIAnalysis.from_api_response(result)
            if not analysis.explanation:
                self.debug('Log Detective returned no explanation; skipping ai_summary.')
                return None
            return analysis

        try:
            response = _request_with_retry('POST', submit_url, body)
        except Exception as exc:
            if plan_limited:
                return None
            self._endpoint_unavailable = True
            self.warn('Log Detective submission timed out or failed, disabling further calls this '
                      'run: {}'.format(exc), sentry=True)
            return None

        if response.status_code != 202:
            return None

        task = _decode_json(response)
        if task is None:
            return None

        # An idempotent POST retry may return an operation that has already completed.
        if task.get('status') in ('done', 'error', 'cancelled'):
            return _analysis_from_task(task)

        location = response.headers.get('Location')
        if not location:
            self.warn('Log Detective accepted the analysis but did not return a Location header.')
            return None

        task_url = urljoin('{}/'.format(base_url), location)
        base_parts = urlsplit(base_url)
        task_parts = urlsplit(task_url)
        if (task_parts.scheme, task_parts.netloc) != (base_parts.scheme, base_parts.netloc):
            self.warn('Log Detective returned a task Location outside the configured server.')
            return None

        retry_after = _retry_after_seconds(
            response.headers.get('Retry-After'), retry_tick)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.warn('Log Detective analysis task did not finish before the retry timeout.', sentry=True)
                return None

            time.sleep(min(retry_after, remaining))
            if time.monotonic() >= deadline:
                self.warn('Log Detective analysis task did not finish before the retry timeout.', sentry=True)
                return None

            try:
                response = _request_with_retry('GET', task_url)
            except Exception as exc:
                if plan_limited:
                    return None
                self._endpoint_unavailable = True
                self.warn('Log Detective task polling timed out or failed, disabling further calls '
                          'this run: {}'.format(exc), sentry=True)
                return None

            if response.status_code != 200:
                return None

            task = _decode_json(response)
            if task is None:
                return None

            status = task.get('status')
            if status in ('done', 'error', 'cancelled'):
                return _analysis_from_task(task)
            if status not in ('scheduled', 'in_progress', 'cancelling'):
                self.warn('Log Detective returned an unknown task status: {}'.format(status))
                return None

            retry_after = _retry_after_seconds(
                response.headers.get('Retry-After'), retry_tick)

    def _analyze_testcase(
            self,
            schedule_entry: 'TestScheduleEntry',
            test_case: TestCase,
    ) -> Optional[AIAnalysis]:
        """
        Return the cached analysis for a single failing testcase.

        The report module rebuilds the test suite (and its testcases) on both the ``execute()``
        and ``destroy()`` serialization passes, so the cache is keyed on the persistent schedule
        entry plus the testcase's stable identity (name, serial number, guest) rather than the
        transient object.

        :param schedule_entry: The tmt schedule entry the testcase belongs to.
        :param test_case: The failing testcase to analyze.
        :returns: Parsed Log Detective analysis as an :py:class:`AIAnalysis`, or ``None`` on
            skip/failure.
        :rtype: AIAnalysis or None
        """
        guest_name = test_case.guest.name if test_case.guest else None
        cache_key = ('testcase', schedule_entry, test_case.name, test_case.serial_number, guest_name)

        if cache_key in self._complete_analyses:
            return self._analysis_cache[cache_key]

        if self._remaining_budget() <= 0 or self.dryrun_enabled or self._endpoint_unavailable:
            return self._analysis_cache.get(cache_key)

        selected = self._build_files_for_testcase(schedule_entry, test_case)
        if not selected:
            self.warn('No log files available for test {} in plan {}; skipping Log Detective analysis.'.format(
                test_case.name, schedule_entry.plan))
            return self._analysis_cache.get(cache_key)

        return self._analyze_files(cache_key, selected, self._build_metadata(schedule_entry, test_case=test_case))

    def _analyze_files(
            self,
            cache_key: Any,
            selected: List[Dict[str, str]],
            metadata: Dict[str, Any],
            schedule_entry: Optional['TestScheduleEntry'] = None,
    ) -> Optional[AIAnalysis]:
        """Reuse a provisional result until more logs are ready or a selected URL changes.

        The ready signature records the named URLs behind each successful analysis. A synthetic
        plan error has an empty URL signature and remains provisional.
        """
        selected_urls = tuple((entry['name'], entry['url']) for entry in selected if 'url' in entry)
        ready = self._ready_files(selected)
        ready_urls = tuple((entry['name'], entry['url']) for entry in ready if 'url' in entry)

        cached = self._analysis_cache.get(cache_key)
        # An unavailable URL cannot improve an existing real-log analysis. The synthetic
        # fallback is useful only until the first successful analysis is available.
        if not ready and cached is not None:
            return cached
        if not ready and schedule_entry is not None:
            error_file = self._synthetic_error_file(schedule_entry)
            if error_file:
                ready = [error_file]

        previous = self._analysis_sources.get(cache_key)
        if previous and selected_urls == previous[0] and len(ready_urls) <= len(previous[1]):
            return cached
        if not ready:
            return cached

        response = self._call_logdetective(ready, metadata)
        if response is None:
            return cached

        self._analysis_cache[cache_key] = response
        self._analysis_sources[cache_key] = (selected_urls, ready_urls)
        if selected_urls and len(selected_urls) == len(ready_urls):
            self._complete_analyses.add(cache_key)
        return response

    def _analyze_suite_fallback(
            self,
            schedule_entry: 'TestScheduleEntry',
    ) -> Optional[AIAnalysis]:
        """
        Return the cached plan-level analysis, retrying as logs become available.

        Used when a plan has no failing testcases to analyze individually (e.g. it errored during
        provisioning or guest-setup before any test ran). The result is stored on the suite.

        :param schedule_entry: The tmt schedule entry to analyze at plan level.
        :returns: Parsed Log Detective analysis as an :py:class:`AIAnalysis`, or ``None`` on
            skip/failure.
        :rtype: AIAnalysis or None
        """
        cache_key = ('suite', schedule_entry)

        if cache_key in self._complete_analyses:
            return self._analysis_cache[cache_key]

        if self._remaining_budget() <= 0 or self.dryrun_enabled or self._endpoint_unavailable:
            return self._analysis_cache.get(cache_key)

        selected = self._build_files_for_suite_fallback(schedule_entry)
        if not selected:
            self.warn('No log files available for plan {}; skipping Log Detective analysis.'.format(
                schedule_entry.plan))
            return self._analysis_cache.get(cache_key)

        return self._analyze_files(cache_key, selected, self._build_metadata(schedule_entry), schedule_entry)

    @staticmethod
    def _eligible_entry(schedule_entry: 'TestScheduleEntry') -> bool:
        """Include tmt failures even when an infrastructure error left the result undefined."""
        return schedule_entry.runner_capability == 'tmt' and (
            schedule_entry.result in (TestScheduleResult.FAILED, TestScheduleResult.ERROR)
            or schedule_entry.state == TestScheduleEntryState.ERROR
        )

    def prepare_final_results(self) -> None:
        """Publish archive inputs before analysis and leave the final report for archive.destroy."""
        if self.dryrun_enabled or self._endpoint_unavailable:
            return
        if self.has_shared('test_schedule'):
            schedule = self.shared('test_schedule') or []
            if not any(self._eligible_entry(entry) for entry in schedule):
                return
        if self.has_shared('publish_before_final_report'):
            try:
                self.shared('publish_before_final_report')
            except Exception as exc:
                # The normal archive teardown must still run even if this early sync fails.
                self.warn('Could not publish artifacts before Log Detective analysis: {}'.format(exc), sentry=True)

    def serialize_test_schedule_entry_results(
            self,
            schedule_entry: 'TestScheduleEntry',
            test_suite: TestSuite,
    ) -> None:
        """
        Overloads the tmt runner's ``serialize_test_schedule_entry_results`` shared function.

        Chains to the underlying provider first (so ``test_suite.test_cases`` is fully populated),
        then requests AI analysis from Log Detective for failed/errored tmt plans. Each failing
        testcase is analyzed on its own logs and gets its own ``ai_summary``; a plan that
        produced no failing testcases falls back to a single plan-level analysis stored on the suite.
        """
        self.overloaded_shared('serialize_test_schedule_entry_results', schedule_entry, test_suite)

        if not self._eligible_entry(schedule_entry):
            return

        if self.has_shared('is_final_results_generation') and not self.shared('is_final_results_generation'):
            # Progress reports rebuild suites too. Carry completed analyses forward without
            # making API calls, including if a timer tick was already waiting on the report lock.
            for test_case in test_suite.test_cases:
                if test_case.result not in ('failed', 'error', 'needs_inspection'):
                    continue
                guest_name = test_case.guest.name if test_case.guest else None
                key = ('testcase', schedule_entry, test_case.name, test_case.serial_number, guest_name)
                test_case.ai_summary = self._analysis_cache.get(key)
            if not any(tc.result in ('failed', 'error', 'needs_inspection') for tc in test_suite.test_cases):
                test_suite.ai_summary = self._analysis_cache.get(('suite', schedule_entry))
            return

        try:
            previous_plan = self._active_plan
            self._active_plan = schedule_entry
            if schedule_entry not in self._plan_deadlines:
                self._plan_deadlines[schedule_entry] = time.monotonic() + (
                    self.option('plan-timeout') or DEFAULT_PLAN_TIMEOUT)
            non_passed = [tc for tc in test_suite.test_cases
                          if tc.result in ('failed', 'error', 'needs_inspection')]

            if non_passed:
                # Analyze each failing testcase individually so its summary reflects its own failure.
                for test_case in non_passed:
                    analysis = self._analyze_testcase(schedule_entry, test_case)
                    if analysis:
                        test_case.ai_summary = analysis
            else:
                # No failing testcases (e.g. the plan errored before any test ran); analyze the
                # plan as a whole and attach the result to the suite.
                analysis = self._analyze_suite_fallback(schedule_entry)
                if analysis:
                    test_suite.ai_summary = analysis
        except Exception as exc:
            self.warn('Unexpected error during Log Detective analysis: {}'.format(exc))
            return
        finally:
            self._active_plan = previous_plan
