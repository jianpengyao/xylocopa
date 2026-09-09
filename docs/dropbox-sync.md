# Dropbox sync

One-way backup of registered xylocopa projects to the user's Dropbox through
the Dropbox HTTP API v2. No desktop client, no changes to project folders.

The feature is **off by default**. It is switched on per project from the
project's Settings card; the first time any project is enabled the UI walks
the user through linking a Dropbox app (OAuth 2 + PKCE) and then lets them
pick which top-level folders of that project to sync.

## Concepts

| Term | Meaning |
|---|---|
| Link | The orchestrator holds a refresh token for one Dropbox account. Global, shared by all projects. |
| App key | Client id of a Dropbox app the user creates (App Folder access). Not a secret; stored with the link. There is no app secret anywhere. |
| Project sync | Per-project switch (`Project.dropbox_sync`) plus a folder selection (`Project.dropbox_folders`) and extra ignore rules (`Project.dropbox_ignore`). |
| Run | One pass over all enabled projects: scan → diff → upload (→ optional prune). Interval-scheduled, plus manual "Sync now". |
| Remote layout | `/<project-name>/<relative path>` inside the app folder, i.e. `Dropbox/Apps/<AppName>/<project-name>/...`. |

## Files on disk

All under `DROPBOX_SYNC_DIR` (default `data/dropbox/`, gitignored with the rest of `data/`):

| File | Contents | Perms |
|---|---|---|
| `token.json` | `{app_key, refresh_token, access_token, expires_at, account_id, account_name, email}` | `0600` |
| `config.json` | Global runtime config (see below) | `0600` |
| `state.db` | SQLite state table for incremental sync | `0600` |

## Global config (`config.json`, edited via `PUT /api/dropbox/config`)

| Key | Default | Meaning |
|---|---|---|
| `paused` | `false` | "Pause" sets it (the current run stops after its in-flight batch); "Resume" clears it. Linking alone makes the scheduler active. |
| `interval_minutes` | `0` | Fallback periodic check interval (0--1440). `0` disables the timer; syncing is purely event-driven. Replaces the former `interval_hours` (migrated automatically on load: minutes = hours * 60). |
| `concurrency` | `4` | Concurrent uploads. |
| `chunk_mb` | `8` | Upload-session chunk size (multiple of 4 MiB). |
| `max_file_mb` | `2048` | Files larger than this are skipped (reported as `too_large`). |
| `max_files_per_project` | `300000` | Budget; a project over budget is reported and skipped until narrowed. |
| `allowlist_mode` | `false` | If true, only files whose extension is in `allowlist_exts` are synced. |
| `allowlist_exts` | code/docs list | Used only in allowlist mode. |
| `prune` | `false` | Propagate local deletions to Dropbox (`files/delete_batch`). |
| `bandwidth_kbps` | `0` | Upload throttle; `0` = unlimited. |

## Ignore rules

Evaluated in this order; a path is skipped if any rule matches. Symlinks are
never followed and never uploaded.

1. Folder selection: only the top-level entries listed in `Project.dropbox_folders`
   (`null` = everything). The pseudo-entry `.` stands for files directly in the
   project root.
2. Built-in defaults (gitignore syntax): `.git/`, `*venv*/`, `.venv/`, `node_modules/`,
   `__pycache__/`, `.cache/`, `*.egg-info/`, `build/`, `dist/`, `torch_home/`,
   `.thumbcache/`, `wandb/`, `.trash/`, `.xylo-internal/`, `*.pyc`, `.DS_Store`.
3. `<project>/.xylocopa-syncignore` (gitignore syntax, optional).
4. `Project.dropbox_ignore` — extra rules edited in the UI.
5. Budgets: `max_file_mb`, allowlist mode.

Matching uses `pathspec` (`gitwildmatch`).

## Upload engine

- Syncing is event-driven: an agent Write/Edit/Bash triggers a check 90 s
  later (5 min for projects over 100k files); an agent turn ending triggers
  a check after 20 s; an attachment upload triggers a check after 5 s;
  enabling a project, linking, or startup triggers an immediate check.
  `interval_minutes` is an optional fallback timer (default 0 = off) for
  changes made outside xylocopa (editor, git pull) -- otherwise those are
  picked up at the next event or "Sync now". A run row is recorded only when
  there was work.
- Scan runs in a worker thread (`asyncio.to_thread`) with `os.scandir`, never
  following symlinks.
- Diff against `state.db`: unchanged `(size, mtime_ns)` → skip without reading;
  otherwise compute the Dropbox `content_hash` (SHA-256 per 4 MiB block, then
  SHA-256 of the concatenated digests) and skip if it equals the stored hash.
- Every file is uploaded through an upload session: `upload_session/start`
  (`close=true` when the whole file fits in one ≤150 MB request), otherwise
  `append_v2` in `chunk_mb` chunks. Sessions are committed in groups with
  `upload_session/finish_batch` (`mode=overwrite`, `mute=true`, `client_modified`
  from the local mtime) and `finish_batch/check` — this is what Dropbox
  recommends for bulk uploads because each `files/upload` is a separate
  namespace write and trips `too_many_write_operations`.
- Retries: exponential backoff on 429/5xx honouring `Retry-After`; a 401
  refreshes the access token once and retries.
- Resumable: open sessions are recorded in `state.db` (`session_id`, `offset`,
  `created_at`) so a restart continues where it stopped; a stale offset is
  corrected from Dropbox's `incorrect_offset` reply, sessions older than six
  days (Dropbox expires them after seven) are restarted from scratch.
- A file that changes while it is being uploaded is not committed — it is
  retried on the next run — and every commit carries the local `content_hash`
  so Dropbox rejects a torn upload.
- Dropbox paths are case-insensitive. When two local paths differ only by
  case, the lexicographically first one is synced and the others are reported
  as skipped collisions in the status.
- Prune (opt-in): state rows whose local file is gone are deleted remotely in
  `files/delete_batch` groups.
- Renaming a project moves its remote folder (`files/move_v2`) and its state
  rows; deleting a project drops its state rows but leaves the remote files.
- The engine never blocks the API: all HTTP is `httpx.AsyncClient`, file reads
  and hashing run in threads, and a semaphore bounds concurrency.

## State schema (`state.db`)

```sql
CREATE TABLE files (
  project TEXT NOT NULL, rel_path TEXT NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  content_hash TEXT NOT NULL, remote_rev TEXT,
  uploaded_at TEXT NOT NULL,
  PRIMARY KEY (project, rel_path)
);
CREATE TABLE pending_sessions (
  project TEXT NOT NULL, rel_path TEXT NOT NULL,
  session_id TEXT NOT NULL, offset INTEGER NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  content_hash TEXT,                -- NULL while bytes are still being appended
  created_at TEXT NOT NULL,
  PRIMARY KEY (project, rel_path)
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL,             -- running | ok | error | cancelled | interrupted
  trigger TEXT NOT NULL,            -- schedule | manual
  project TEXT,                     -- project being processed
  files_scanned INTEGER DEFAULT 0, files_uploaded INTEGER DEFAULT 0,
  bytes_uploaded INTEGER DEFAULT 0, files_deleted INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, error_sample TEXT
);
CREATE TABLE project_stats (
  project TEXT PRIMARY KEY, files_synced INTEGER, bytes_synced INTEGER,
  last_synced_at TEXT, last_error TEXT
);
CREATE TABLE errors (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, project TEXT, path TEXT, message TEXT NOT NULL
);                                  -- kept to the most recent 200 rows
```

## Database columns (`projects` table)

| Column | Type | Meaning |
|---|---|---|
| `dropbox_sync` | `BOOLEAN NOT NULL DEFAULT 0` | Per-project switch. |
| `dropbox_folders` | `TEXT` | JSON array of selected top-level entries; `NULL` = all. |
| `dropbox_ignore` | `TEXT` | Extra gitignore-style rules. |

Exposed on `ProjectOut` and editable through `PATCH /api/projects/{name}/settings`.

## HTTP API

| Method | Path | Effect |
|---|---|---|
| `GET` | `/api/dropbox/status` | Link state, account, space usage, config, current run progress, last run, per-project stats, queue length, recent errors. |
| `PUT` | `/api/dropbox/config` | Update global config (validated, persisted to `config.json`, wakes the scheduler). |
| `POST` | `/api/dropbox/link/start` | Body `{mode: "redirect" \| "code", return_to?}` → `{authorize_url, redirect_uri, mode}`. Uses the configured `DROPBOX_APP_KEY`; generates the PKCE verifier and keeps it in memory. |
| `GET` | `/api/dropbox/callback` | OAuth redirect target (`code`, `state`). Auth-exempt; completes the exchange and 302s back to `return_to` with `?dropbox=linked` or `?dropbox=error&dropbox_message=…`. |
| `POST` | `/api/dropbox/link/complete` | Body `{code}` → exchanges a pasted code (fallback mode), stores the token, returns the account. |
| `DELETE` | `/api/dropbox/link` | Revoke the token and delete `token.json`. Remote files are left alone. |
| `POST` | `/api/dropbox/sync` | Body `{project?}` → start a run now (whole account or one project). |
| `POST` | `/api/dropbox/pause` / `/resume` | Pause / resume the scheduler (a running run finishes its current file batch and stops). |
| `POST` | `/api/dropbox/dry-run` | Body `{project, folders?, ignore?}` → `{job_id}`. The scan runs in a thread and streams per-top-level-entry `{files, bytes, skipped}` without uploading. |
| `GET` | `/api/dropbox/dry-run/{job_id}` | Job progress: `status` (`running`/`complete`/`error`), `entries` so far, `total` when complete. Jobs nobody polls for 30 s are stopped. |
| `DELETE` | `/api/dropbox/dry-run/{job_id}` | Stop a running dry run. |
| `GET` | `/api/projects/{name}/dropbox/folders` | Top-level entries of a project (`name, type, default_ignored, selected`) for the folder picker. |
| `GET` | `/api/projects/{name}/dropbox/status` | Cheap per-project view (link state, selection, counts, last error, progress when this project is being synced) used by the project settings row. |

## OAuth flow (PKCE, no app secret)

Three link modes are available; `POST /api/dropbox/link/start` with
`{"mode": "auto"}` (the default) picks the right one automatically.

### Relay mode (default for the project app)

The project ships a fixed redirect URI page at
`https://jianpengyao.github.io/xylocopa/oauth/dropbox/` (source:
`docs/oauth/dropbox/index.html`). Any self-hosted xylocopa instance can
link without registering its own redirect URI.

1. `POST /api/dropbox/link/start` with `{"mode": "auto"}` (or `"relay"`).
   The orchestrator generates the Dropbox authorize URL with the relay URL
   as `redirect_uri` and returns a `relay_start_url` — the relay page URL
   with a `#return=<origin>&authorize=<authorize_url>` fragment.
2. The browser navigates to the relay page (same tab). The page stores the
   instance origin (`return`) in `localStorage`, then redirects to the
   Dropbox authorize URL.
3. After the user signs in and approves, Dropbox redirects to the relay
   page with `?code=…&state=…`. The page reads the stored origin and
   sends the browser to `<origin>/api/dropbox/callback?code=…&state=…`.
4. The callback exchanges the code (with the relay URL as `redirect_uri`
   in the token exchange, since Dropbox requires it to match), stores
   tokens, and redirects to the `return_to` path with `?dropbox=linked`.

The relay page stores only the instance origin in the visitor's browser;
it never redirects to an address taken from the callback URL and makes
no network calls of its own.

**What the relay page stores:** the key `xylocopa.dropbox.return` in
`localStorage`, containing the instance origin (`https://host:port`).
The key is cleared after the return redirect.

**Self-hosting the relay page:** set `DROPBOX_RELAY_URL` in `.env` to
point at your own copy (you will also need to register that URL as a
redirect URI on your Dropbox app).

### Direct mode (own Dropbox app)

When the user sets their own `DROPBOX_APP_KEY` in `.env` (and no
`DROPBOX_RELAY_URL` override), `auto` selects `direct` mode. The
orchestrator uses `<origin>/api/dropbox/callback` as the redirect URI.

1. Register redirect URIs on the Dropbox app for every origin:
   `https://<host>:3000/api/dropbox/callback`.
2. `POST /api/dropbox/link/start` derives the redirect URI from the
   request origin and returns `{"mode": "direct", "authorize_url", …}`.
3. Browser → Dropbox → `GET /api/dropbox/callback?code=…&state=…` →
   token exchange → redirect to `return_to?dropbox=linked`.

### One-time developer setup (own app)

1. Create a Dropbox app at <https://www.dropbox.com/developers/apps>:
   *Scoped access → App folder*.
2. Under **Permissions**, enable `files.metadata.read`, `files.content.read`,
   `files.content.write`, `account_info.read`. Submit.
3. Under **OAuth 2 → Redirect URIs**, add
   `https://<host>:3000/api/dropbox/callback` for each origin you use.
4. Copy the **App key** and set it in `.env`:

   ```
   DROPBOX_APP_KEY=<your app key>
   ```

   Then restart the orchestrator.

### Paste-code fallback

`link/start` with `{"mode": "code"}` omits the `redirect_uri`. Dropbox
shows a code for the user to copy/paste. `POST /api/dropbox/link/complete`
with `{"code": "…"}` exchanges it. Useful when neither the relay page
nor a registered redirect URI is available.

### Token lifecycle

- Access tokens are refreshed with `grant_type=refresh_token` when expired.
- Unlink calls `/2/auth/token/revoke` then deletes `token.json`.
- A started link flow expires after ten minutes; start again if the code
  is pasted later than that.

### Maintainer notes

- Register `https://jianpengyao.github.io/xylocopa/oauth/dropbox/` as a
  redirect URI on the project Dropbox app.
- Dropbox production status is required once the app exceeds 50 linked
  users.

## MCP tools (read-only)

Names follow the verb whitelist in `docs/agent-mcp-tools.md`.

- `dropbox_get()` — link state, schedule, last run, per-project counts and recent errors.
- `dropbox_count(project)` — dry run: files and bytes that would sync per top-level folder. Nothing is uploaded.

## Web UI

- **Project → Settings → Dropbox Sync** row: toggle. Turning it on when no
  account is linked opens the link dialog first, then the folder picker
  (multi-select with *Select all* / *Deselect all*, per-folder file/size
  counts from a dry run, extra ignore rules). When on, the row shows the
  selection summary and last sync time with *Folders…* and *Sync now*.
- **Monitor → Dropbox** card: account, space, scheduler config, last/current
  run, per-project counts, errors, *Sync now* / *Pause* / *Unlink*.

## v2 (designed, not built)

Two-way sync: `files/list_folder` + `/continue` cursors per project namespace,
`files/list_folder/longpoll` to wake on remote changes, download changed files
into the workspace with conflict copies (`name (conflicted copy <date>).ext`)
when the local file changed since the last known state, never touching ignored
paths. The `files` table already carries `remote_rev` for this.
