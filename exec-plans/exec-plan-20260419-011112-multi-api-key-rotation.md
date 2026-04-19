# Add Multi-Key API Rotation For Groq Whisper

This ExecPlan is a living document. During execution, `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept in sync with the actual state.

## Purpose / Big Picture

The Windows app currently stores exactly one Groq API key locally and sends that single key to the backend when a transcription session starts. The backend then creates one Groq client for the whole session and uses that same key for every rolling transcription request. Users who own multiple keys cannot pool their effective request budget across those keys.

After this work, the Settings page will let the user manage multiple Groq API keys, the frontend will send the full key list to the backend when starting transcription, and the backend will distribute transcription requests across those keys. The first implementation should optimize for correctness and predictable behavior rather than hidden heuristics: users should be able to confirm that multiple keys are accepted and stored locally, while request distribution itself is primarily confirmed by automated tests and developer verification rather than a new end-user diagnostic surface.

### Goal Specification

**Scenario: Save multiple keys in Settings**
- Given the user opens `ui/GroqWhisper/Pages/SettingsPage.xaml`
- When they paste multiple Groq API keys into the editor using one key per line and click Save
- Then the keys are encrypted and stored locally for the current Windows user
- And the page reports how many keys are stored
- And reopening the page allows the stored keys to be revealed back into the editor

**Scenario: Start transcription with multiple keys**
- Given multiple keys are stored locally and the backend is idle
- When the user clicks Start on the Live page
- Then the frontend sends a list of keys to `POST /start`
- And the backend accepts the list and enters the running state
- And the session behaves like the existing single-key flow from the user’s perspective

**Scenario: Request distribution**
- Given the backend is actively producing rolling transcription requests
- When more than one API key was supplied at session start
- Then transcription requests are assigned in round-robin order across the supplied keys
- And if one request fails with a retry-eligible upstream error such as rate limiting or temporary service failure, the backend retries once with the next key before surfacing an error

**Scenario: Failover advances the global cursor**
- Given three keys `A`, `B`, and `C` in the active session
- When a request attempts `A`, receives a retry-eligible error, and succeeds on retry with `B`
- Then the next brand-new transcription request starts with `C`
- And the pool does not rewind to `A` after a successful failover

**Scenario: Existing single-key local secret after the direct cut**
- Given the Windows user still has the old single-key `groq-api-key.dat` payload from a prior build
- When the new Settings page tries to reveal or use local API keys
- Then the app treats that payload as unsupported by the new format
- And the user gets an actionable message telling them to re-enter and save keys in the new multi-key format
- And the implementation does not silently reinterpret the old payload as a one-item list

**Scenario: `/settings` remains secret-free**
- Given the backend exposes `/settings`
- When the frontend or any caller sends `api_keys` to `PUT /settings`
- Then the backend rejects the request as secret material
- And `GET /settings` never returns `api_keys`

**Scenario: No distinct failover target**
- Given the active pool contains only one distinct credential after normalization
- When a transcription request hits a retry-eligible upstream error
- Then the backend does not retry that request against the same credential again
- And the original retry-eligible error is surfaced immediately

**Scenario: Direct cut to the new start contract**
- Given the frontend and backend in this repository are updated together
- When a start request is sent
- Then the intended contract is `api_keys: string[]`
- And the first implementation does not preserve the old single-string `api_key` request format

| Dimension | Before | After |
|-----------|--------|-------|
| Local secret storage | One encrypted string in `groq-api-key.dat` | Multiple encrypted keys stored as one logical secret payload |
| Settings UI | Single key editor with reveal/hide semantics | Multi-line editor, one key per line, with stored key count and reveal/hide for the full list |
| Frontend start payload | Sends `api_key` string | Sends `api_keys` string array |
| Backend client usage | One Groq client for the entire session | A small Groq client pool used in round-robin order per request |
| Rate-limit handling | First upstream limit/error fails the session | One retry on the next key for retry-eligible upstream failures |

- Correctness metric: with three stored keys, six consecutive transcription requests should use key order `1, 2, 3, 1, 2, 3` in tests.
- Resilience metric: a retry-eligible failure on one key should succeed when the next key succeeds, measured by backend unit tests.
- Failover sequencing metric: with keys `A, B, C`, a request sequence of `A(fail) -> B(success)` must make the next fresh request start with `C`.
- Secret-format metric: a legacy raw-string payload must be rejected, while a new-format payload containing one key still loads successfully.
- Security constraint: keys remain locally encrypted at rest via the existing DPAPI-backed `WindowsSecretStore`.

## Progress

- [x] (2026-04-19T01:09:52-07:00) Explored the current single-key path in `ui/GroqWhisper/Pages/SettingsPage.xaml(.cs)`, `ui/GroqWhisper.Core/WindowsSecretStore.cs`, `ui/GroqWhisper/ViewModels/LiveViewModel.cs`, `ui/GroqWhisper/Services/TranscriptionApiClient.cs`, `backend/src/groq_whisper_service/api.py`, `backend/src/groq_whisper_service/service.py`, and `backend/src/groq_whisper_service/rolling_transcriber.py`.
- [x] (2026-04-19T01:09:52-07:00) Captured product decisions from the user: round-robin plus failover retry, direct cut to the new `api_keys` start contract, and Settings UX based on one key per line.
- [x] (2026-04-19T01:11:12-07:00) Created isolated worktree `C:\git\groq-whisper\.worktrees\plan-multi-api-key-rotation` on branch `plan-multi-api-key-rotation`.
- [x] (2026-04-19T01:11:12-07:00) Verified the baseline in the worktree with `dotnet test ui\GroqWhisper.Tests\GroqWhisper.Tests.csproj -p:Platform=x64` and `C:\git\groq-whisper\.venv\Scripts\python.exe -m pytest backend\tests\test_service.py -q`.
- [x] (2026-04-19T01:17:39-07:00) Ran one `$exec-agent-review` pass against the ExecPlan and captured non-minor issues around old local secret compatibility, failover cursor semantics, frontend payload test coverage, and `rolling_transcriber.py` verification scope.
- [x] (2026-04-19T01:17:39-07:00) Captured a final planning decision from the user: old local single-key secret storage is intentionally not supported after the direct cut.
- [x] (2026-04-19T01:17:39-07:00) Folded the review findings back into the ExecPlan by making the local-secret break explicit, defining post-failover cursor movement, and expanding verification coverage.
- [x] (2026-04-19T01:17:39-07:00) Write and polish the ExecPlan document.
- [x] (2026-04-19T01:25:01-07:00) Ran a second full `$exec-agent-review` pass and captured remaining issues around self-describing secret format, single/distinct-key failover semantics, `/settings` rejection coverage for `api_keys`, and verification wording.
- [x] (2026-04-19T01:25:01-07:00) Folded the second review findings back into the ExecPlan by requiring a versioned secret envelope, defining no-distinct-target retry behavior, and tightening `/settings` and verification requirements.
- [x] (2026-04-19T01:30:51-07:00) Ran a third full `$exec-agent-review` pass and captured the last important issues around empty-editor Save semantics and explicit normalization coverage.
- [x] (2026-04-19T01:30:51-07:00) Folded the third review findings back into the ExecPlan by turning empty-editor Save into a single explicit rule and by making normalization and terminology consistency part of planned verification.
- [x] (2026-04-19T01:32:46-07:00) Stopped the attempted fourth review round after the user clarified that only P0 issues may exceed three review rounds. This plan therefore exits the review loop after three rounds, with the last fixes accepted by user instruction rather than a fourth reviewer pass.
- [x] (2026-04-19T01:39:44-07:00) Resumed execution after the user explicitly asked to implement the plan with milestone-level reviews capped at three rounds unless a P0 issue remains.
- [x] (2026-04-19T01:39:44-07:00) Implemented Milestone 1 in `ui/GroqWhisper.Core/WindowsSecretStore.cs`, `ui/GroqWhisper/Pages/SettingsPage.xaml(.cs)`, and `ui/GroqWhisper.Tests/WindowsSecretStoreTests.cs` by switching local secret storage to a versioned multi-key envelope and updating the Settings page to edit, reveal, clear, and report plural API keys.
- [x] (2026-04-19T01:39:44-07:00) Verified the Milestone 1 code path with `dotnet test ui/GroqWhisper.Tests/GroqWhisper.Tests.csproj -p:Platform=x64` and `dotnet build ui/GroqWhisper.sln -p:Platform=x64`.
- [x] (2026-04-19T01:48:44-07:00) Ran Milestone 1 review round 1 against the full diff from `79bc9891c0a71baedfd388cb634234711ecbe7bf` to `cc4ed81` and captured one important issue around malformed-envelope recovery plus one minor Settings status-text issue.
- [x] (2026-04-19T01:48:44-07:00) Fixed the round-1 findings by rejecting malformed JSON envelopes with null entries as unsupported format, by preventing empty-editor Save from claiming unchanged keys when no usable local keys exist, and by re-running the Milestone 1 .NET test/build verification with zero warnings.
- [x] (2026-04-19T01:56:07-07:00) Ran Milestone 1 review round 2 against the full diff from `79bc9891c0a71baedfd388cb634234711ecbe7bf` to `82791817552e70ff7ee3bfca89c512469f59d70f` and captured one important issue in the unsupported-format recovery flow plus one minor reminder about the current lack of automated Settings-page behavior coverage.
- [x] (2026-04-19T01:56:07-07:00) Fixed the round-2 recovery issue by making `Reveal` open an empty editor even when local secret loading fails, with actionable guidance telling the user to paste replacement API keys and save. Re-verified the milestone with `dotnet test ui/GroqWhisper.Tests/GroqWhisper.Tests.csproj -p:Platform=x64` and `dotnet build ui/GroqWhisper.sln -p:Platform=x64`.
- [x] (2026-04-19T02:04:25-07:00) Ran Milestone 1 review round 3 against the full diff from `79bc9891c0a71baedfd388cb634234711ecbe7bf` to `c5db3aa` and captured one important issue around file-access failures during eager key counting plus one minor issue around long recovery text wrapping.
- [x] (2026-04-19T02:04:25-07:00) Fixed the round-3 findings by surfacing file-access failures as actionable local-secret read errors, by adding a regression test that locks the secret file during `LoadGroqApiKeys()`, and by enabling wrapping for long recovery/status text in the Settings page. Re-verified the milestone with `dotnet test ui/GroqWhisper.Tests/GroqWhisper.Tests.csproj -p:Platform=x64` and `dotnet build ui/GroqWhisper.sln -p:Platform=x64`.
- [x] (2026-04-19T02:04:25-07:00) Stopped the Milestone 1 review loop after three rounds because no P0 issue remained and the user explicitly capped non-P0 review loops at three rounds.
- [x] (2026-04-19T02:06:39-07:00) Implemented Milestone 2 in `ui/GroqWhisper.Core/TranscriptionStartRequest.cs`, `ui/GroqWhisper.Tests/TranscriptionStartRequestTests.cs`, `ui/GroqWhisper/Services/TranscriptionApiClient.cs`, and `ui/GroqWhisper/ViewModels/LiveViewModel.cs` by moving the frontend `/start` payload to `api_keys` and routing serialization through a Core DTO that tests can validate directly.
- [x] (2026-04-19T02:06:39-07:00) Verified the Milestone 2 frontend contract with `dotnet test ui/GroqWhisper.Tests/GroqWhisper.Tests.csproj -p:Platform=x64` and `dotnet build ui/GroqWhisper.sln -p:Platform=x64`, both passing with zero warnings.

## Surprises & Discoveries

- Observation: The repo currently has no tracked `exec-plans/` directory; the default plan location must be created as a new untracked directory.
  Evidence: `Get-ChildItem exec-plans -Force` failed in the repository root before plan creation.

- Observation: `.git/info/exclude` already contains `.worktrees/`, so the workflow-dev worktree can be created without changing ignore rules.
  Evidence: `Get-Content .git\info\exclude` includes `.worktrees/`.

- Observation: The current backend start contract hard-requires a single non-empty `api_key` string in `POST /start`, and the frontend mirrors that assumption.
  Evidence: `backend/src/groq_whisper_service/api.py` rejects missing or non-string `api_key`; `ui/GroqWhisper/Services/TranscriptionApiClient.cs` builds a `Dictionary<string, string>` with only `api_key`.

- Observation: The current service creates one Groq client per session at start time, so adding more stored keys alone would not change runtime request distribution.
  Evidence: `backend/src/groq_whisper_service/service.py` resolves one API key in `start()` and assigns `self.client = self.client_factory(resolved_api_key)`.

- Observation: The live product surface was recently simplified to `Start / Stop`, but the backend still exposes `pause` and `resume`.
  Evidence: `ui/GroqWhisper/Pages/LivePage.xaml` now exposes only Start and Stop; `backend/src/groq_whisper_service/api.py` still includes `/pause` and `/resume`.

- Observation: `backend/tests/test_stable_prefix.py` imports and exercises `rolling_transcriber.py`, so changes to request-routing helpers in that module can create regressions outside `test_service.py`.
  Evidence: `rg -n "rolling_transcriber|create_client|transcribe_bytes|load_api_key" backend\tests` returns multiple references inside `backend/tests/test_stable_prefix.py`.

- Observation: the current .NET test project references only `ui/GroqWhisper.Core`, not the WinUI app project, so frontend start-payload coverage must be planned deliberately instead of assumed.
  Evidence: `ui/GroqWhisper.Tests/GroqWhisper.Tests.csproj` currently contains only a `ProjectReference` to `..\GroqWhisper.Core\GroqWhisper.Core.csproj`.

- Observation: a newline-joined plaintext storage format would make an old single-key raw string indistinguishable from a valid new one-key save, so “direct incompatibility” requires a self-describing envelope.
  Evidence: both old and naive-new one-key payloads would decrypt to a single UTF-8 string unless the new format includes an explicit marker such as JSON structure or version metadata.

- Observation: the Settings-page changes can be compile-verified from the solution build, but there is still no automated WinUI interaction test project in this repository.
  Evidence: `dotnet build ui\GroqWhisper.sln -p:Platform=x64` passed after the page refactor, while `ui/GroqWhisper.Tests` continues to cover only `GroqWhisper.Core`.

- Observation: `System.Text.Json` will deserialize `null` array elements inside the stored envelope even when the target property is a string list, so malformed local secrets must validate list contents explicitly instead of assuming every entry is non-null.
  Evidence: Milestone 1 review round 1 identified that a payload such as `{"Version":1,"ApiKeys":[null,"b"]}` would otherwise reach `Trim()` and throw `NullReferenceException`.

- Observation: a recoverable error message is not enough for the Settings page if the key editor remains hidden; the unsupported-format path also has to leave the replacement editor visible so the user can act immediately.
  Evidence: Milestone 1 review round 2 found that `Reveal` surfaced the error text but kept the multi-line editor collapsed, blocking direct overwrite of the bad local payload.

- Observation: eager reads added for stored-key counts can fail on ordinary file access problems such as a locked or temporarily unreadable secret file, not just on decryption or format issues.
  Evidence: Milestone 1 review round 3 identified that `File.ReadAllBytes` failures could still escape Settings-page initialization until the store started wrapping read-access exceptions as user-visible recovery errors.

- Observation: the current .NET test project constraint was real in practice: the easiest way to get automated frontend contract coverage without referencing the WinUI assembly was to move the `/start` body shape into `GroqWhisper.Core` as a pure serializable DTO.
  Evidence: Milestone 2 added `TranscriptionStartRequest` under `ui/GroqWhisper.Core` and exercised it directly from `ui/GroqWhisper.Tests`, while the WinUI project continued to build unchanged from the test project’s perspective.

## Decision Log

- Decision: The first implementation will support true runtime rotation, not only multi-key storage.
  Rationale: The user explicitly wants larger effective rate-limit capacity, which requires per-request key distribution rather than just storing several keys.
  Date/Author: 2026-04-19 / Codex + user

- Decision: The first implementation will use round-robin request assignment and retry once on the next key for retry-eligible upstream errors.
  Rationale: The user selected “轮询+失败切换”. This gives immediate value beyond naive round-robin while keeping retry behavior bounded and testable.
  Date/Author: 2026-04-19 / Codex + user

- Decision: The frontend/backend start contract will move directly to `api_keys: string[]` rather than preserving the old `api_key` field.
  Rationale: The user selected “直接切新格式”. The repository frontend and backend are developed together, so the direct-cut contract change is acceptable in this branch.
  Date/Author: 2026-04-19 / Codex + user

- Decision: The Settings page will use a multi-line text editor with one key per line.
  Rationale: The user selected “每行一个 key”. It fits the current WinUI app better than introducing a new add/remove list model in the first pass.
  Date/Author: 2026-04-19 / Codex + user

- Decision: Encryption at rest remains the existing DPAPI-backed model.
  Rationale: The feature changes cardinality, not the security boundary. Reusing `ISecretProtector` and `DpapiSecretProtector` avoids introducing new secret handling risk.
  Date/Author: 2026-04-19 / Codex

- Decision: The direct cut applies to local secret format as well as the `/start` contract. Existing single-key local payloads are intentionally unsupported and will require user re-entry.
  Rationale: The user explicitly selected “直接不兼容” for old local secret storage during planning. The plan must make that breakage visible instead of leaving it implicit.
  Date/Author: 2026-04-19 / Codex + user

- Decision: The new stored plaintext will use a self-describing, versioned envelope rather than a newline-joined raw string.
  Rationale: Without an explicit format marker, the new one-key representation would be impossible to distinguish from the legacy raw-string payload, which would make the intended incompatibility unenforceable.
  Date/Author: 2026-04-19 / Codex

- Decision: After a retry-eligible failure, the pool cursor advances past the successful retry key. Example: `A(fail) -> B(success)` means the next request starts at `C`.
  Rationale: This keeps the global sequence deterministic and avoids overusing early keys after failover.
  Date/Author: 2026-04-19 / Codex

- Decision: Normalization removes blank lines and exact duplicate credentials while preserving first-seen order, so failover only ever targets a distinct key.
  Rationale: Duplicate keys do not increase rate-limit capacity and would otherwise create ambiguous retry behavior such as reusing the same credential twice.
  Date/Author: 2026-04-19 / Codex

- Decision: If normalization leaves only one distinct key, retry-eligible failures do not trigger same-key retry within the pool.
  Rationale: “Retry on the next key” should mean failover to a different credential, not duplicate submission against the same key.
  Date/Author: 2026-04-19 / Codex

- Decision: Saving Settings with an empty multi-line key editor never changes the stored keys. It still saves non-secret backend settings, then shows a status message that keys were left unchanged and that `Clear` is the only removal path.
  Rationale: This removes implementation ambiguity while preserving the existing dedicated destructive clear control.
  Date/Author: 2026-04-19 / Codex

- Decision: The multi-key local secret is persisted as a JSON envelope with `Version` and `ApiKeys` fields, and legacy raw-string payloads fail fast with an actionable unsupported-format message instead of silent reinterpretation.
  Rationale: The implemented storage must make the direct incompatible cut enforceable while still allowing a new-format one-key payload to round-trip correctly.
  Date/Author: 2026-04-19 / Codex

- Decision: Malformed JSON envelopes, including envelopes that deserialize to null list entries, are treated as unsupported format rather than crashing the Settings page.
  Rationale: Users need a recoverable error path for bad local secret payloads, and the Settings page calls local-secret loading during page initialization.
  Date/Author: 2026-04-19 / Codex

- Decision: When `Reveal` fails to load the stored keys, the Settings page still opens an empty multi-line editor and tells the user to paste replacement API keys before saving.
  Rationale: The unsupported-format and decrypt-failure paths need an actionable in-place recovery flow, not just an error banner.
  Date/Author: 2026-04-19 / Codex

- Decision: File-access failures while reading the stored secret are surfaced through the same recoverable UI path as other local-secret load failures instead of being allowed to crash eager Settings-page reads.
  Rationale: The page now reads the stored secret during load to report key counts, so transient file-read failures need a stable user-facing error path.
  Date/Author: 2026-04-19 / Codex

- Decision: The frontend `/start` contract is implemented through a dedicated Core DTO with JSON property annotations rather than ad hoc dictionary construction in the HTTP client.
  Rationale: This keeps the `api_keys` payload shape explicit, makes serialization testable from the existing test project, and reduces the chance of drifting back to the old `api_key` field by accident.
  Date/Author: 2026-04-19 / Codex

- Decision: Frontend payload-shape automation will be added through a small pure request body helper or DTO in `ui/GroqWhisper.Core`, so `ui/GroqWhisper.Tests` can verify `api_keys` serialization without taking a dependency on the WinUI app assembly.
  Rationale: The existing test project already references `GroqWhisper.Core` but not `GroqWhisper`, so this is the lowest-friction path to automated frontend contract coverage.
  Date/Author: 2026-04-19 / Codex

- Decision: The CLI path in `rolling_transcriber.py` remains single-key and environment-driven for this milestone; multi-key pooling is a session-start concern for the Windows app/backend service path.
  Rationale: The user-facing feature request is about the desktop app’s stored keys and live service session. Preserving the CLI’s one-key contract reduces unrelated surface-area change while the routing helper is introduced for service use.
  Date/Author: 2026-04-19 / Codex

- Decision: User-visible and service-visible paths touched by this feature use plural terminology (`API keys`, `api_keys`) consistently, while unchanged CLI-only single-key paths may retain singular naming as an explicit exception.
  Rationale: The contract is moving from one secret to many, and the plan should not permit a half-migrated mix of singular and plural terminology across the app and service.
  Date/Author: 2026-04-19 / Codex

- Decision: The planning review loop is capped at three rounds unless a P0 issue remains.
  Rationale: The user explicitly clarified that important or minor findings do not justify a fourth review round.
  Date/Author: 2026-04-19 / Codex + user

## Outcomes & Retrospective

Milestone 1 is implemented and passes its automated verification. After the third and final allowed review round, the Windows-side multi-key storage flow now covers the main recovery cases called out during review: legacy raw-string payloads, malformed JSON envelopes, decrypt failures, file-read failures, empty-editor saves, and the “reveal a bad payload then immediately replace it” path all now degrade to actionable UI guidance instead of hidden or crashy failure modes. Milestone 2 is now also implemented and verified locally: the Windows frontend’s `/start` request is no longer built around a single `api_key` string, and the test suite now has direct regression coverage for `api_keys` JSON serialization through the new Core DTO. The remaining gaps are still the backend-side contract update and runtime rotation logic, plus manual walkthrough confirmation after the end-to-end path is complete.

## Context and Orientation

This repository has two main deliverables:

1. `ui/` contains the Windows desktop app. The relevant files for this feature are:
   - `ui/GroqWhisper/Pages/SettingsPage.xaml` and `.cs`: current UI for storing one API key locally and editing backend settings.
   - `ui/GroqWhisper/ViewModels/LiveViewModel.cs`: loads the stored key and sends it during `StartAsync()`.
   - `ui/GroqWhisper/Services/TranscriptionApiClient.cs`: owns the `/start` request payload shape.
   - `ui/GroqWhisper.Core/WindowsSecretStore.cs`: encrypts and stores the key material under LocalApplicationData.
   - `ui/GroqWhisper.Tests/WindowsSecretStoreTests.cs`: current .NET unit coverage for local secret storage.

2. `backend/` contains the FastAPI service and rolling transcription logic. The relevant files are:
   - `backend/src/groq_whisper_service/api.py`: translates HTTP JSON bodies into service start/stop/config calls.
   - `backend/src/groq_whisper_service/service.py`: the state machine that starts capture, creates the Groq client, and drives each rolling transcription request.
   - `backend/src/groq_whisper_service/rolling_transcriber.py`: the Groq SDK integration and request construction layer.
   - `backend/tests/test_service.py`: main Python coverage for service transitions and API endpoint behavior.

The key architectural constraint is that the Windows app owns user secrets and only hands them to the backend at session start. The backend does not persist secrets in `/settings`, and that separation should remain true after the feature.

## Milestones

### Milestone 1: Multi-key storage and editing in the Windows app

After this milestone, the user can paste multiple keys into Settings, save them locally, clear them, and reveal them back into the editor. The UI clearly indicates that one key is expected per line and how many keys are currently stored. Verification is via `WindowsSecretStore` unit tests plus a manual Settings page walkthrough. Completion requires the old single-key editor code paths to be removed or updated so the UI and storage semantics are internally consistent, requires an explicit unsupported-old-format message for legacy single-key local payloads, requires the new persisted plaintext to use a versioned envelope that can distinguish a new one-key save from the old raw-string payload, and requires the empty-editor Save path to leave stored keys unchanged while still permitting non-secret settings to be saved.

### Milestone 2: Frontend-to-backend contract moves to `api_keys`

After this milestone, clicking Start on the Live page sends a list of keys rather than a single key string. The backend `/start` endpoint validates the new list payload and rejects empty or malformed lists with actionable errors, while `/settings` continues rejecting all secret fields including `api_keys`. Verification is via backend API tests, a frontend-side payload-shape test that asserts `api_keys` serialization, and a local desktop app startup check. Completion requires all frontend start paths in this repo to use the new contract consistently.

### Milestone 3: Runtime round-robin rotation with bounded failover

After this milestone, each transcription request uses the next key in the pool, and retry-eligible upstream failures attempt one retry using the next distinct key. Verification is via Python tests that assert request ordering, fallback behavior, the cursor position after a failover success, and the no-distinct-key case. Completion requires the rotation logic to be deterministic, thread-safe for the current service model, and able to surface a clear terminal error when all allowed attempts fail without changing the existing CLI’s single-key usage path.

### Milestone 4: Full verification and user-visible confirmation

After this milestone, both the .NET and Python test suites covering the changed areas pass, the desktop app builds, and the plan document reflects the final implementation and findings. Verification is via the listed automated test commands plus a manual Settings -> Start demo. Completion requires the final user-visible behavior to match the Goal Specification and the ExecPlan’s living sections to be updated.

## Work Plan

Begin in `ui/GroqWhisper.Core/WindowsSecretStore.cs` by changing the serialized secret payload from a single plaintext string to a structured list format suitable for multiple keys. The new plaintext must use a self-describing versioned envelope, not a newline-joined raw string. The storage layer must normalize whitespace, drop blank lines, remove exact duplicates while preserving first-seen order, reject empty final payloads, and continue using the existing DPAPI protector abstraction. Because the user explicitly chose a direct incompatible cut, the load path should detect old single-string payloads and surface a clear “re-enter and save keys in the new format” error instead of auto-upgrading them. Update `ui/GroqWhisper.Tests/WindowsSecretStoreTests.cs` to verify multi-key round-trip, overwrite behavior, empty-input rejection, unsupported-old-format behavior, acceptance of a valid new-format single-key payload, and normalization examples such as `\"  A  \", \"\", \"A\", \"B\" -> [\"A\", \"B\"]`, along with decryption failure handling.

Next, refactor the Settings page in `ui/GroqWhisper/Pages/SettingsPage.xaml(.cs)` from a single-key password-style editor to a multi-line key editor. Preserve the current operational pattern where saved keys are not automatically revealed into the form on page load; “Reveal” should explicitly load the stored keys into the editor, and “Hide” should clear the in-memory editor contents. Update the status strings to talk about key counts instead of one stored key, make the empty-editor Save behavior non-destructive, and make that rule explicit in the status text: Save with no keys leaves stored keys untouched while still allowing non-secret settings to be applied; only `Clear` removes stored keys.

Then, add frontend contract coverage and update the Live transport path together. Extract the start request body shape into a small pure helper or DTO under `ui/GroqWhisper.Core` so `ui/GroqWhisper.Tests` can assert the emitted JSON uses `api_keys`. After that, update `ui/GroqWhisper/ViewModels/LiveViewModel.cs` to load the full key list and pass it to `ui/GroqWhisper/Services/TranscriptionApiClient.cs`, whose `PostStartAsync` method should emit `api_keys` as a JSON array. Ensure the missing-key error shown to the user refers to the absence of stored keys rather than a singular key.

On the backend side, change `backend/src/groq_whisper_service/api.py` and `backend/src/groq_whisper_service/service.py` so `/start` and `RealtimeTranscriptionService.start()` accept a non-empty key list. Keep the invariant that `/settings` refuses secret material, including the new `api_keys` field, and ensure `GET /settings` never returns it. The service should validate and normalize the list once per start call, then pass the normalized list into pooled-client creation. The backend-side tests must assert normalization explicitly, not only downstream round-robin behavior after normalization has already occurred.

Implement the actual rotation in `backend/src/groq_whisper_service/rolling_transcriber.py` or a new small helper module adjacent to it, but keep the CLI-oriented `load_api_key()` and `run_once()` / `run_rolling()` path single-key and environment-driven. The service-side design should be a thin wrapper around one Groq client per key that exposes the same `client.audio.transcriptions.create(...)` surface the rest of the code already expects. That wrapper should hold a lock-protected round-robin index, advance the global cursor past the successful retry key, and be the only place that decides which underlying key is used for each request. Add a targeted retry policy there: for retry-eligible upstream exceptions, try exactly one more distinct key before failing; if no distinct key exists after normalization, surface the original retry-eligible failure without same-key retry. Keep error classification explicit and conservative; if the SDK exception shape is ambiguous, inspect the real exception attributes and document the chosen predicate in the Decision Log during implementation.

Finally, update backend tests in `backend/tests/test_service.py` and `backend/tests/test_stable_prefix.py` or add an equivalent direct routing-helper test module so the modified `rolling_transcriber.py` surface is covered from both the service path and its existing module consumers. Those tests must explicitly cover `/settings` rejecting `api_keys`, successful failover to the next distinct key, the single-distinct-key no-retry case, and normalization examples such as `[\"  A  \", \"\", \"A\", \"B\"] -> [\"A\", \"B\"]`. Rebuild the desktop app and re-run the changed test suites from the worktree.

## Detailed Steps

1. Create `exec-plans/` in the worktree and keep this plan updated while work proceeds.
2. Record the milestone base SHA before implementation work begins:

   ```powershell
   git -C C:\git\groq-whisper\.worktrees\plan-multi-api-key-rotation rev-parse HEAD
   ```

3. Update local secret storage:

   ```powershell
    code ui\GroqWhisper.Core\WindowsSecretStore.cs
    code ui\GroqWhisper.Tests\WindowsSecretStoreTests.cs
    dotnet test ui\GroqWhisper.Tests\GroqWhisper.Tests.csproj -p:Platform=x64
    ```

4. Update Settings and frontend start-payload flow:

   ```powershell
    code ui\GroqWhisper\Pages\SettingsPage.xaml
    code ui\GroqWhisper\Pages\SettingsPage.xaml.cs
    code ui\GroqWhisper.Core
    code ui\GroqWhisper\ViewModels\LiveViewModel.cs
    code ui\GroqWhisper\Services\TranscriptionApiClient.cs
    dotnet test ui\GroqWhisper.Tests\GroqWhisper.Tests.csproj -p:Platform=x64
    dotnet build ui\GroqWhisper.sln -p:Platform=x64
    ```

5. Update backend start contract and rotation logic:

   ```powershell
    code backend\src\groq_whisper_service\api.py
    code backend\src\groq_whisper_service\service.py
    code backend\src\groq_whisper_service\rolling_transcriber.py
    C:\git\groq-whisper\.venv\Scripts\python.exe -m pytest backend\tests\test_service.py -q
    C:\git\groq-whisper\.venv\Scripts\python.exe -m pytest backend\tests\test_stable_prefix.py -q
    ```

6. Run combined verification and a manual app smoke test from the worktree.

Failure handling guidance:
- If `dotnet build` fails because the desktop app executable is locked, stop the running `GroqWhisper.exe` process tree first, then rerun the build.
- If backend retry classification proves unclear from tests, add a temporary diagnostic unit test or local exception probe before finalizing the predicate.
- If the direct-cut `api_keys` contract breaks an overlooked frontend caller inside this repository, update that caller in the same milestone rather than restoring the old field.
- If users report unsupported old local secret payloads after the direct cut, that behavior is expected for this milestone and should be messaged clearly rather than silently repaired.

## Verification and Acceptance

Automated verification must include:

```powershell
dotnet test ui\GroqWhisper.Tests\GroqWhisper.Tests.csproj -p:Platform=x64
C:\git\groq-whisper\.venv\Scripts\python.exe -m pytest backend\tests\test_service.py -q
C:\git\groq-whisper\.venv\Scripts\python.exe -m pytest backend\tests\test_stable_prefix.py -q
dotnet build ui\GroqWhisper.sln -p:Platform=x64
```

Manual verification must include:

1. Launch the desktop app from the worktree build output.
2. Open Settings, paste three keys on separate lines, save, navigate away, return, and use Reveal to confirm the same three keys load back into the editor.
3. If an old single-key local payload exists, confirm the page shows the explicit unsupported-format guidance rather than silently treating it as a valid one-item list.
4. Start a transcription session and confirm that the app transitions to Running with no “missing key” error.
5. Use the automated backend tests as the authoritative proof for request distribution and failover semantics, including the no-distinct-key case.

Acceptance criteria:
- The user can manage multiple keys in the Settings page without manual file editing.
- The direct-cut behavior for old local single-key payloads is explicit and actionable.
- The frontend and backend agree on the `api_keys` array start contract.
- `/settings` continues rejecting all secret fields, including `api_keys`, and never returns them.
- Save with an empty key editor does not remove stored keys and communicates that `Clear` is the only removal path.
- Normalization behavior for whitespace, blank lines, duplicates, and ordering is explicitly covered by tests at both the local-storage and backend-start boundaries.
- Runtime request selection is deterministic and covered by tests.
- Retry-on-next-key behavior is covered by tests and does not spin indefinitely.
- Secrets remain excluded from `/settings` payloads and continue to be stored only on the Windows side.

Consistency requirement:
- User-visible and service-visible strings, fields, and tests touched by this feature should use plural terminology (`API keys`, `api_keys`) consistently. Unchanged CLI-only code may retain singular naming only where the plan explicitly preserves the single-key path.

## Artifacts and Notes

Baseline evidence captured during planning:

```text
dotnet test ui\GroqWhisper.Tests\GroqWhisper.Tests.csproj -p:Platform=x64
Passed!  - Failed: 0, Passed: 9, Skipped: 0, Total: 9

C:\git\groq-whisper\.venv\Scripts\python.exe -m pytest backend\tests\test_service.py -q
40 passed in 2.28s
```

Worktree and branch prepared for this plan:

```text
Worktree: C:\git\groq-whisper\.worktrees\plan-multi-api-key-rotation
Branch: plan-multi-api-key-rotation
Base HEAD at planning time: 6866da25d03afa6b3b55b7fb2b8e0f60cd64b7bf
```

## Interfaces and Dependencies

- `ui/GroqWhisper.Core/WindowsSecretStore.cs`: local encrypted persistence boundary for user secrets.
- `ui/GroqWhisper/Pages/SettingsPage.xaml(.cs)`: WinUI settings editor and status messaging for stored keys and backend settings.
- `ui/GroqWhisper/ViewModels/LiveViewModel.cs`: entry point that loads stored keys and starts sessions.
- `ui/GroqWhisper/Services/TranscriptionApiClient.cs`: HTTP client for backend API calls; owns the start-request JSON shape.
- `backend/src/groq_whisper_service/api.py`: FastAPI layer validating JSON request bodies.
- `backend/src/groq_whisper_service/service.py`: backend state machine and long-running session orchestration.
- `backend/src/groq_whisper_service/rolling_transcriber.py`: Groq SDK client creation and request execution surface; this is the right layer for round-robin routing and bounded failover.
- Groq Python SDK (`groq` package): upstream transcription client library used by the backend.
- DPAPI via `DpapiSecretProtector`: Windows-only encryption mechanism already used for local secret storage.
