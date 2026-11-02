Log Detective analysis
======================

The ``log-detective`` module adds AI failure explanations to Testing Farm xUnit
results. Load it after the tmt result serializer so it can inspect the completed
testcases. Testcases with ``failed``, ``error``, or ``needs_inspection`` outcomes
are analyzed separately. A plan with a failed or error result, or an error
state (even if its result is still undefined), is eligible for analysis. An
eligible plan without failed testcases receives a suite-level analysis when
suitable input exists.

For Testing Farm pipelines using ``archive``, load ``archive`` before
``test-schedule-report`` and load ``log-detective`` after
``test-schedule-tmt`` but before ``test-schedule-report``. During teardown, the
report module asks Log Detective to stop the archive progress timer. Before
analysis, Log Detective asks the archive module to synchronously run its
configured ``destroy``-stage sync once. The archive module's later ``destroy``
pass syncs again to upload the final ``results.xml`` containing the AI
summaries and any files created during teardown. The first sync must include
the local logs selected for analysis; the second sync is normally incremental
for unchanged artifacts. The first sync redacts a copy of each source so
processes with open log files can continue writing through their original file
descriptors. Both syncs use the configured archive backend, destination map,
and secret hiding. For rsync, a directory source ending in ``/`` copies its
contents into the configured destination; the temporary copy used by the first
sync keeps this behavior. A source without that slash copies the directory
itself. The extra sync adds archive time to the reporting path.
If the archive module is absent, URL reachability checks still apply but local
logs are not published by Log Detective.

The report module generates results during execute without analysis. It runs
analysis only during destroy, after the first archive publication. Runner and
archive progress reports do not contact Log Detective, even when they publish
current test results. They reuse any cached analyses so a later progress write
does not remove AI summaries from ``results.xml``.
Only regular log files are submitted; directory
links remain in the normal test results but are excluded from analysis requests.
The ``artifact-limit`` option sets the maximum number of logs selected for each
analysis (default 15). It accepts any positive integer; choose a value the
configured Log Detective server accepts. The testcase output and journal take
priority when the limit is small. Zero and negative values are rejected when
the module starts.
Auxiliary ``data/`` files are included only when a small sample is valid UTF-8
text without NUL bytes. This check can omit logs in other encodings.
Local logs are submitted by artifact URL, regardless of size, so Log Detective
fetches the complete files without loading them into the reporting process.
When tmt uses ``tmt-run.log`` as a testcase's ``testout.log`` because results
are missing, that one source file is submitted only once.
URL artifacts retain their original contents, including any secrets in those
files. A synthetic ``error.txt`` is submitted as inline content when a plan has
no log URLs or none of its log URLs are reachable. The shared
``hide-secrets.redact_text`` function redacts that synthetic text, artifact names,
and build metadata before submission. If the
redactor is unavailable, analysis is skipped so metadata and synthetic error
text cannot be submitted without redaction.
When ``api-token`` is configured, the module registers it with
``hide-secrets.add_secrets`` during execute. Authenticated requests are skipped
if token registration is unavailable or fails.
Remote log URLs must be HTTP(S) URLs accepted by Log Detective.
URLs containing a registered secret are omitted because replacing the secret
would change the location.

The ``plan-timeout`` option limits artifact checks, submissions, and polling
for each eligible plan to 180 seconds by default. Every plan gets its own budget;
there is no run-wide analysis cap, so reporting time can grow with the number
of eligible plans. The ``retry-timeout`` option defaults to 120 seconds per task
and is capped by the remaining plan budget. These limits bound Log Detective
analysis time during final reporting; the pre-report archive sync adds its own
time before analysis starts.
For each testcase or plan,
the module waits up to 30 seconds within that plan's budget for URL artifacts to
become reachable. The check falls back to a streamed GET for servers that do
not support HEAD. Each pass checks every URL still unavailable, but polling
stops as soon as ``testout.log`` is reachable (or the first URL artifact if
there is no ``testout.log``). Other logs can therefore still be unavailable.
Only URLs reachable from the pipeline worker at check time are submitted;
this does not guarantee that the Log Detective server can fetch them later.
If pre-report publication fails, the module can still use URLs already reachable
from an earlier progress sync.
Synthetic error text needs no availability check. The module skips testcase
analysis if no URLs are ready.

When a plan's budget expires or Log Detective fails, its remaining AI summaries
are omitted. A slow analysis task does not prevent analysis of later plans;
transport retry exhaustion may still mark the endpoint unavailable for the run.
Test outcomes and Testing Farm request completion are unaffected.
API-provided explanation, solution, snippet text, and snippet source filename
are stripped of XML 1.0-forbidden characters before entering the result model.
Valid tabs and line breaks are preserved. A response whose explanation becomes
empty after cleaning is omitted.

Timeouts while waiting for artifacts or analysis, and exhaustion of a plan's
budget, are reported to Sentry through module warnings.

The client supplies a UUID for each submission to make HTTP retries idempotent.
The Log Detective API also accepts requests without an ID and assigns one in its
response.
