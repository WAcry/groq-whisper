# ExecPlan: Groq Whisper Desktop Product

## Objective

Transform the existing groq-whisper streaming transcription backend into a complete desktop product:
1. **Backend**: Add state machine, control endpoints, SQLite session persistence
2. **Frontend**: WinUI 3 application with Fluent 2 design (real-time view, history, devices, settings)
3. **Integration**: Git repo setup, process management, SSE subscription

## Constraints & Decisions

- **Platform**: We are developing on Linux. WinUI 3 code will be written and structured correctly but cannot be compiled/tested here. Backend Python code can be fully tested.
- **No algorithm changes**: The audio capture, mixing, and stable-prefix aggregation are battle-tested. We only add a management layer around them.
- **SQLite for persistence**: Lightweight, no server needed, perfect for desktop.
- **No CORS needed**: WinUI's native `HttpClient` makes direct HTTP calls (not browser-origin-bound). Only add CORS if WebView2 is used later.

## Success Criteria

1. Backend boots into `idle` state; `/healthz` returns `ok` (process alive); `/state` returns `idle` (not recording)
2. All control endpoints (`/start`, `/stop`, `/pause`, `/resume`, `/devices`, `/settings`, `/sessions`) work correctly
3. Sessions are persisted to SQLite with full text, timestamps, model, export_path
4. WinUI 3 project is a valid Windows App SDK solution with four navigation pages
5. Real-time page shows committed text (solid) vs tail text (dimmed) with incremental patch updates
6. History page lists past sessions with full transcript view
7. Existing backend tests continue to pass: `cd backend && python -m pytest tests/ -v`

---

## Milestones

### Milestone 1: Git Setup

**Goal**: Initialize parent-level git repo with proper ignore rules.

**Acceptance criteria**:
- `/home/coder/git/groq-whisper/.git` exists as the single repo
- `.gitignore` excludes `playground/`, `GROQ_APIKEY`, `.venv/`, `__pycache__/`, etc.
- Nested `backend/.git` and `playground/.git` are removed (content preserved)
- Initial commit includes existing backend source

**Steps**:
1. Remove `backend/.git` and `playground/.git` directories
2. `git init` at `/home/coder/git/groq-whisper`
3. Create `.gitignore`:
   ```
   playground/
   GROQ_APIKEY
   .venv/
   __pycache__/
   *.pyc
   *.pyo
   *.egg-info/
   dist/
   build/
   recordings/
   artifacts/
   .env
   *.log
   ```
4. `git add backend/ ui/ AGENTS.md exec-plans/ .gitignore && git commit`

**Verification**: `git status` shows clean working tree, `git log --oneline` shows initial commit.

---

### Milestone 2: Backend State Machine

**Goal**: Add service state management with clear separation between "process health" and "recording state".

**Key design decision** (addresses Codex finding #1):
- `/healthz` = "is the process alive and API reachable?" Always `ok` when FastAPI is running, regardless of recording state. This is what the UI polls to know the backend launched successfully.
- `/state` = the recording lifecycle: `idle`, `preflight`, `running`, `paused`, `error`

**Key design decision** (addresses Codex finding #2 — pause/resume):
- Pause does NOT kill the worker thread. Instead, a `_paused` threading.Event flag is checked inside `_run_loop()`. When set, the loop skips Groq API calls but keeps the thread alive (no `finally` trigger, no flush, no capture stop).
- Resume clears the flag. The next tick proceeds normally. Audio captured during pause is in the rolling buffer and will be included in the next window if within retention.
- The aggregator and tick_index continue from where they left off. No session break on pause.

**Acceptance criteria**:
- `ServiceState` enum with: `idle`, `preflight`, `running`, `paused`, `error`
- Lifespan no longer auto-calls `start()` — service starts in `idle`
- `start()`: `idle` -> `preflight` (validate API key, check ffmpeg via `subprocess`) -> `running`
- `stop()`: `running`/`paused` -> `idle`
- `pause()`: `running` -> `paused` (set `_paused` event)
- `resume()`: `paused` -> `running` (clear `_paused` event)
- Invalid transitions return errors (not exceptions)
- `/healthz` returns `{"status": "ok"}` whenever FastAPI is up
- `/state` returns `{"state": "idle", ...}` with full snapshot

**Steps**:
1. Add `ServiceState` enum to `service.py`
2. Add `_paused` Event and `_state` field to `__init__`
3. Modify `_run_loop()`: check `_paused` flag at each tick — if set, `self.stop_event.wait(0.5)` and `continue` (skip Groq call, don't exit loop)
4. Implement `start()` with preflight checks:
   - Validate API key loads successfully
   - Check `ffmpeg -version` subprocess exits 0
   - Only proceed to `running` if preflight passes; otherwise -> `error`
5. Modify `stop()` to flush aggregator, stop capture, transition to `idle`
6. Add `pause()` and `resume()` methods with state guards
7. Update `create_app()` lifespan: don't call `start()`, only ensure `stop()` on shutdown
8. Update `/healthz` to always return `ok` when process is up
9. Update `/state` to return `self._state.value` plus existing snapshot fields
10. Run `cd backend && python -m pytest tests/ -v`

---

### Milestone 3: Control Endpoints

**Goal**: Add REST endpoints for UI control.

**Key design decision** (addresses Codex finding #4 — `/devices` Windows dependency):
- `audio_capture.py` imports `pyaudiowpatch` at module level (line 15), which fails on non-Windows. The `/devices` endpoint must lazy-import the capture module and catch `ImportError`/`OSError`.
- First version: return current default pair + flat device list. No custom pairing UI yet.

**Acceptance criteria**:
- `POST /start` with optional `{model, language, prompt}` body
- `POST /stop`, `POST /pause`, `POST /resume` with state validation
- `GET /devices` returns device list (or empty + message on non-Windows)
- `GET /settings` returns config; `PUT /settings` updates (only in `idle`)
- All return `{"ok": bool, "state": str, "error"?: str}`
- Unit tests for each endpoint

**Steps**:
1. Add `POST /start`:
   - Accept optional JSON body with `model`, `language`, `prompt` fields
   - Call `service.update_config(...)` if fields provided, then `service.start()`
   - Return `{"ok": true, "state": "running"}` or `{"ok": false, "state": "error", "error": "..."}`
2. Add `POST /stop`, `POST /pause`, `POST /resume`:
   - Each calls the corresponding service method
   - Return state-validated response
3. Add `GET /devices`:
   ```python
   @app.get("/devices")
   def devices():
       try:
           from .audio_capture import ContinuousDualAudioCapture
           cap = ContinuousDualAudioCapture.__new__(ContinuousDualAudioCapture)
           # Use PyAudio to enumerate devices
           import pyaudiowpatch as pyaudio
           p = pyaudio.PyAudio()
           # ... enumerate and return
       except (ImportError, OSError):
           return JSONResponse({"devices": [], "error": "Audio device enumeration not available on this platform"})
   ```
4. Add `GET /settings` returning `dataclasses.asdict(service.config)`
5. Add `PUT /settings` accepting partial JSON, merging into config (only when `idle`)
6. Add `update_config()` method to service for safe config replacement
7. Write tests: test each endpoint, test invalid state transitions return 409

---

### Milestone 4: SQLite Session Persistence

**Goal**: Persist transcription sessions to local SQLite database.

**Key design decision** (addresses Codex finding #3 — finalize location):
- Persistence hooks go into `_publish()`, NOT into `stop()`. This is because:
  - `transcription.final` is emitted from `_run_loop()` finally block (service.py:357-373)
  - `service.error` is emitted via `_publish_error()` (service.py:276-279)
  - `stop()` only signals and joins (service.py:214-225)
- Flow: `create_session` on entering `running` state. `update_text` on each `transcription.patch` in `_publish()`. `finalize_session` on `transcription.final` or `service.error` in `_publish()`.

**Key design decision** (addresses Codex finding #6 — missing schema fields):
- Add `export_path` (nullable) to sessions table per AGENTS.md line 62.

**Acceptance criteria**:
- `sessions` table: `id`, `started_at`, `ended_at`, `model`, `language`, `prompt`, `full_text`, `error_log`, `duration_seconds`, `tick_count`, `export_path`
- Session created when state enters `running`
- `full_text` updated on each `transcription.patch` via `_publish()` hook
- Session finalized on `transcription.final` or `service.error` via `_publish()` hook
- `GET /sessions` returns list (newest first, paginated with `?limit=&offset=`)
- `GET /sessions/{id}` returns full session
- `DELETE /sessions/{id}` removes session
- DB path: `~/.groq-whisper/sessions.db` (auto-created)

**Steps**:
1. Create `backend/src/groq_whisper_service/persistence.py`:
   ```python
   class SessionStore:
       def __init__(self, db_path: Path | None = None): ...
       def create_session(self, *, model, language, prompt) -> str: ...
       def update_text(self, session_id: str, *, full_text: str, tick_count: int): ...
       def finalize_session(self, session_id: str, *, ended_at, full_text, error_log, duration_seconds, tick_count): ...
       def get_session(self, session_id: str) -> dict | None: ...
       def list_sessions(self, *, limit=50, offset=0) -> list[dict]: ...
       def delete_session(self, session_id: str) -> bool: ...
       def update_export_path(self, session_id: str, export_path: str): ...
   ```
2. Schema with `CREATE TABLE IF NOT EXISTS` for safe init
3. Add `session_store` parameter to `RealtimeTranscriptionService.__init__()` (optional, default None)
4. Hook into `_publish()`:
   - If event type is `transcription.patch`: call `session_store.update_text()`
   - If event type is `transcription.final`: call `session_store.finalize_session()`
   - If event type is `service.error`: call `session_store.finalize_session()` with error_log
5. Hook into `start()`: call `session_store.create_session()`, store `self._current_session_id`
6. Add session endpoints to `api.py`
7. Wire `SessionStore` in `create_app()` — inject into service
8. Write tests: test CRUD operations, test publish hook triggers persistence

---

### Milestone 5: WinUI 3 Frontend — Project Scaffold & Navigation

**Goal**: Create the WinUI 3 project with shell navigation and backend process management.

**Key design decision** (addresses Codex finding #5 — backend launch):
- UI launches backend via `backend/serve.py` (the actual entry point that adds `src/` to sys.path)
- Working directory set to `backend/` relative to the UI executable
- UI ships with a known Python installation path (configurable in settings)

**Key design decision** (addresses Codex finding #6 — status bar):
- Status bar items per AGENTS.md line 20: Groq API status, ffmpeg availability, mic name, speaker name, network status
- Backend must expose these in `/state` response. Add `preflight_results` field to state snapshot.

**Acceptance criteria**:
- Valid WinUI 3 / Windows App SDK project in `ui/`
- Shell window with NavigationView (Live, History, Devices, Settings)
- `BackendService.cs` launches `python serve.py` and polls `/healthz`
- `TranscriptionApiClient.cs` wraps all REST + SSE endpoints

**Steps**:
1. Create project structure:
   ```
   ui/
   ├── GroqWhisper.sln
   └── GroqWhisper/
       ├── GroqWhisper.csproj
       ├── Package.appxmanifest
       ├── App.xaml / App.xaml.cs
       ├── MainWindow.xaml / MainWindow.xaml.cs
       ├── Pages/
       │   ├── LivePage.xaml / .cs
       │   ├── HistoryPage.xaml / .cs
       │   ├── DevicesPage.xaml / .cs
       │   └── SettingsPage.xaml / .cs
       ├── ViewModels/
       │   ├── LiveViewModel.cs
       │   ├── HistoryViewModel.cs
       │   └── SettingsViewModel.cs
       ├── Services/
       │   ├── BackendService.cs
       │   └── TranscriptionApiClient.cs
       ├── Models/
       │   ├── ServiceState.cs
       │   ├── TranscriptionPatch.cs
       │   └── Session.cs
       └── Assets/
   ```
2. `GroqWhisper.csproj` targeting `net8.0-windows10.0.19041.0` with `Microsoft.WindowsAppSDK` 1.5+
3. `MainWindow.xaml`: NavigationView with Fluent 2 icons, Mica backdrop
4. Page stubs with basic layout placeholders
5. `BackendService.cs`:
   - `LaunchAsync(pythonPath, servePath)` — starts `python serve.py` as child process
   - `WaitForReadyAsync(timeout)` — polls `GET /healthz` every 500ms
   - `ShutdownAsync()` — sends SIGTERM/kills process
   - Reads backend stdout/stderr for logging
6. `TranscriptionApiClient.cs`:
   - `PostStartAsync(model?, language?, prompt?)`
   - `PostStopAsync()`, `PostPauseAsync()`, `PostResumeAsync()`
   - `GetStateAsync()`, `GetDevicesAsync()`, `GetSettingsAsync()`
   - `GetSessionsAsync(limit, offset)`, `GetSessionAsync(id)`, `DeleteSessionAsync(id)`
   - `SubscribeEventsAsync(CancellationToken)` — returns `IAsyncEnumerable<SseEvent>`

---

### Milestone 6: WinUI 3 Frontend — Live Transcription Page

**Goal**: Implement the real-time transcription view with full status bar.

**Acceptance criteria**:
- Text area: committed text (full opacity) + tail text (50% opacity) via RichTextBlock
- Start/Pause/Stop buttons with state-driven enabled/disabled
- Status bar: Groq status, ffmpeg, mic name, speaker name, model, duration, tick count
- Incremental patch application using `replace_from_char` + `replacement_text`
- Model selector: "Speed" (`whisper-large-v3-turbo`) / "Accuracy" (`whisper-large-v3`)
- Copy and Export buttons

**Steps**:
1. `LivePage.xaml`:
   - Top: Status indicators (InfoBars or colored dots for Groq/ffmpeg/mic/speaker)
   - Center: `RichTextBlock` with two `Run` elements (committed + tail)
   - Bottom: `CommandBar` with Start, Pause, Stop, Model picker, Copy, Export
2. `LiveViewModel.cs`:
   - `CommittedText`, `TailText`, `DisplayText` observable properties
   - `State` property driving button IsEnabled bindings
   - `StatusItems` collection for top bar
   - SSE event processing on UI thread via `DispatcherQueue`
3. On `transcription.patch`:
   - Update `CommittedText` and `TailText` from event payload
   - RichTextBlock binding splits: committed in `Foreground="{ThemeResource TextFillColorPrimaryBrush}"`, tail in `Foreground="{ThemeResource TextFillColorTertiaryBrush}"`
4. Model selector `ComboBox` with display names mapping to model IDs
5. Copy: `DataPackage` to clipboard. Export: `FileSavePicker` -> `.txt`

---

### Milestone 7: WinUI 3 Frontend — History & Settings Pages

**Goal**: History browser, device viewer, and settings configuration.

**Acceptance criteria**:
- History page: session list with date, model, duration, text preview
- Session detail view with full transcript and export
- Delete confirmation dialog
- Settings page: API key path, default model, language, advanced audio params
- Devices page: detected mic + speaker with refresh

**Steps**:
1. `HistoryPage.xaml`: `ListView` with `DataTemplate` showing session cards
2. Session detail: navigate to `HistoryDetailPage` or use split-view
3. `HistoryViewModel.cs`: loads via `TranscriptionApiClient.GetSessionsAsync()`, supports pull-to-refresh
4. `SettingsPage.xaml`: Fluent form fields, `FolderPicker` for API key file
5. `DevicesPage.xaml`: `ItemsRepeater` showing devices from `GET /devices`
6. Settings persistence via `ApplicationData.Current.LocalSettings`

---

## Progress

| Milestone | Status |
|-----------|--------|
| 1. Git Setup | Not started |
| 2. Backend State Machine | Not started |
| 3. Control Endpoints | Not started |
| 4. SQLite Session Persistence | Not started |
| 5. WinUI 3 Scaffold & Navigation | Not started |
| 6. Live Transcription Page | Not started |
| 7. History & Settings Pages | Not started |

## Surprises & Discoveries

(To be filled during implementation)

## Decision Log

| # | Decision | Reason |
|---|----------|--------|
| 1 | Single parent-level git repo with backend/ and ui/ siblings | AGENTS.md guidance; simpler than separate repos |
| 2 | Use `sqlite3` stdlib, not SQLAlchemy | Desktop app, single user, no ORM complexity needed |
| 3 | Pause = set flag in worker loop, don't kill thread | Worker's `finally` block flushes aggregator + stops capture; killing thread would trigger unwanted finalization. Flag approach keeps thread alive, skips Groq API calls. |
| 4 | Target `net8.0-windows10.0.19041.0` | Windows 10 2004+ baseline, matches WinUI 3 / Windows App SDK 1.5+ |
| 5 | `/healthz` = process alive; `/state` = recording lifecycle | Frontend needs to distinguish "backend not launched yet" from "backend idle" from "backend error". Healthz is the readiness probe. |
| 6 | Persistence hooks in `_publish()`, not `stop()` | Final text is generated in `_run_loop()` finally block and published as `transcription.final`. `stop()` only signals + joins. Hooking `_publish()` catches all events at the right time. |
| 7 | `/devices` lazy-imports pyaudiowpatch | Module-level import fails on non-Windows. Lazy import with try/except lets the endpoint degrade gracefully. |
| 8 | UI launches backend via `serve.py` | That's the actual entry point that configures sys.path. `python -m groq_whisper_service` only works after `pip install -e .` |
| 9 | No CORS middleware | WinUI native HttpClient doesn't enforce CORS. Only add if WebView2 is used later. |
| 10 | `export_path` nullable column in sessions | AGENTS.md line 62 requires tracking export path in history |

## Outcomes & Retrospective

(To be filled after completion)
