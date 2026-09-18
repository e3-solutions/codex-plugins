# Jolly Roger compatibility preparation

The proposed manifest is `plugins/codex-session-logging/.jollyroger.json`.
It targets the design contract in
[Jolly Roger PR #2](https://github.com/e3-solutions/jolly-roger/pull/2), revision
`b84a762`. A manifest does not activate this plugin: the released manager still
needs a loader/dispatcher, reviewed enrollment, native hook approval, dependency
checks and employee authentication. This change does not enroll a source pin.

The bundle contains the existing Codex logger client and thin managed wrappers.
Forum/Sesh guidance runs for Codex and Claude SessionStart. Capture runs only for
the existing five Codex events. The Claude logger and its additional events,
updater, presence ticker and transcript workers remain standalone. Forum, Sesh
and Cosmos services stay in their owning repositories. The included search skill
does not authenticate to Cosmos or change service permissions.

## Managed invocation

The central runner must invoke Python with `-I -B` to exclude user Python import
configuration. Managed entrypoints bootstrap bundled sibling imports explicitly;
the existing detached drain uses the same isolated flags in managed mode. Tests
poison `PYTHONPATH` and `sitecustomize` to verify alternate code cannot run.

`scripts/jolly_roger_guidance.py` accepts the v1 envelope and emits one JSON
object containing optional context. It calls local Sesh and Forum guidance only.
`scripts/jolly_roger.py` calls existing capture and rollout-sync functions and
returns protocol status without context. Both require matching managed component
and provider variables plus the exact state map below. Invalid/missing maps skip
the hook with a fixed diagnostic before product code runs. Product exceptions
produce a fixed diagnostic, avoiding raw transcript/configuration error text.
No wrapper installs code, edits native hooks/trust or starts an updater.

## State must be mapped before adoption

Jolly Roger owns the local approval for these mappings. It must preserve existing
overrides when present; these defaults are not permission to reset a user's path.

| State id | Existing default / override | Managed translation |
|---|---|---|
| `logger-state` | `$CODEX_HOME/session-logging`, or `CODEX_SESSION_LOG_STATE_DIR` | `CODEX_SESSION_LOG_STATE_DIR` |
| `logger-preferences` | `$CODEX_HOME/session-logging` containing `preferences.json` | `CODEX_SESSION_LOG_PREFERENCES_DIR`, honored only in managed mode |
| `forum-feedback` | `~/.cache/e3-collective`, or `E3_COLLECTIVE_HOOK_STATE_DIR` | `E3_COLLECTIVE_HOOK_STATE_DIR` |

`logger-state` and `logger-preferences` commonly map to the same directory within
this component. They remain separate declarations because existing deployments
can redirect queues while retaining privacy preferences in the provider home.
The map JSON is authoritative; the wrapper neither copies nor moves records.
Queue schemas, raw files, sequence allocation, rollout checkpoints and feedback
debounce remain upstream implementation. Repeated capture retains existing event
semantics: it is not newly promised exactly-once telemetry. Feedback debounce
remains advisory. No state format migration is introduced, so a retained upstream
release can use its same legacy directories on rollback.

The default `JOLLY_ROGER_STATE_DIR` does not replace these reviewed legacy paths.
Provider homes remain unchanged for native transcript/configuration reads.
Tokens, destinations, user identity and collection scope retain their existing
sources. Preserved upload opt-out prevents spawning the uploader; it does not
mean local capture is disabled, matching standalone behavior.

## Detached upload remains an enrollment blocker

Capture hooks declare `background_work: existing-worker`: existing capture writes
the record to its durable queue before launching `drain_queue.py`. The worker
uses the existing nonblocking exclusive queue lock. Foreground `ok` means local
handling completed; it does not establish upload completion. A skipped eligible
capture should be diagnosed, and unrelated coding continues.

Before enabling capture centrally, verify worker ownership, retained release
lifetime, termination/recovery and rollback against actual manager lifecycle.
The local tests establish durable enqueue and lock exclusion under contention;
they do not establish production worker supervision or successful remote upload.
Keep telemetry externally managed until those checks pass. Central event budgets
must accommodate the declared timeouts and native host margin; the manifest
does not select an aggregate budget. Guidance can be tested independently, but
this combined declaration must not be silently partially enrolled.

Before managed adoption, remove or migrate the old combined logger hooks under
explicit central ownership. Running old SessionStart alongside new guidance and
capture would duplicate effects. Preserve unrelated hooks and native trust bytes.
Existing Sherlock clients that share Forum debounce state also require an
explicit shared-state ownership decision; this manifest cannot claim ownership
over another component's store.

## Verification

Run `python3 -B -m unittest discover -s tests -p test_jolly_roger.py -v`.
Tests copy only manifest includes into a temporary bundle, execute real entrypoints
in isolated homes with a fixture E3 Git remote, check all advertised surfaces,
guidance opt-outs/debounce, missing input, retained queue and sequence, disabled
upload preference, and existing drain lock exclusion. Home/config/trust and
bundle file hashes must remain unchanged. No production service is contacted.

Also run the repository regression suite, `python3 -B -m unittest discover -s tests`,
and its pytest tests. Native agent execution/trust, authenticated Cosmos usage,
production ingestion, signed manager updates and real rollout remain pilot gates.
