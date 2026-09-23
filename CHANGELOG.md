# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Each entry mirrors the corresponding [GitHub release](https://github.com/jianpengyao/xylocopa/releases) — see those pages for the full prose write-up. This file keeps the same content in Keep-a-Changelog form so it's grep-able from a clone.

## [Unreleased]

## [0.17.1] - 2026-09-23

Maintenance release for Claude Code 2.1.28x and mobile performance. Claude Code started wrapping every pasted prompt in `<pasted_content>` tags, which made every web-sent message appear twice; the parser now unwraps them and a repair tool cleans up existing history. The chat page on phones was measured under iPhone emulation and its idle cost was cut substantially (network 5.6 MB/min to 118 KB/min, script time to about a third). Also: Opus 5.5 as the default model, and launched sessions no longer have their `opus` alias pinned to Opus 4.6.

### Fixed

- **Duplicate user bubbles with Claude Code 2.1.28x.** Since 2.1.28x the CLI writes pasted input to the session JSONL wrapped in `<pasted_content id="…">…</pasted_content id="…">`. Every orchestrator-delivered message is a paste, so web and task prompts echoed back wrapped, failed the content match, their sent row never got promoted, and a second `source=cli` bubble was created. `jsonl_parser.unwrap_pasted_content` removes the wrapper in the parser, in `strip_agent_preamble` and defensively in `ContentMatcher`. (af95aceb)
- **History purged on compaction of large sessions.** The compact-time full scan reads only the last 50 MB of an oversized session JSONL and treated every older turn as an orphan to delete; a 151 MB session lost about 3,200 agent and system rows over three compactions. The purge is skipped when the scan was truncated. (0bae0ca2)
- **`tools/repair_pasted_content_dupes.py`** merges the duplicate bubbles back into their web/task rows, promotes rows left in the sent state from their JSONL echo, and rebuilds the affected display files (`--apply`, `--rebuild ID[,ID]`). (088c0a63, 9c239f10)
- **`opus` alias pinned to Opus 4.6 in launched sessions.** `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-6[1m]` was exported into every session, so `/model` defaults and `opus` subagents resolved to 4.6 regardless of the agent's model; it is now exported only when the launch itself uses `--model opus`. (0db7e7dd)
- **Composer bar transparent on phones** after the blur removal; touch devices now take the `no-glass` path with each theme's opaque bar colours. (0db7e7dd)

### Changed

- **Chat page polling.** `GET /api/agents/{id}` gained `include_subagents=false`; the 3 s poll uses it, so an agent with hundreds of finished workflow children no longer re-sends a ~260 KB list every tick. Unchanged polls keep the previous state objects instead of re-rendering the page; WebSocket events no longer re-render the chat, tasks and task-detail pages through an unused `lastEvent` state; tool-activity tracking moved to refs; render-path `console.log` calls removed. (a3077f90, f894ac41)
- **Memoized bubbles.** `ChatBubble` is `React.memo`'d with stable handler props and the markdown parse of each bubble is memoized on its content. (0addbf2f)
- **Debug instrumentation off by default.** The console mirror to `POST /api/debug/frontend-log` and the keyboard sampler to `/api/debug/kb-log` are behind `localStorage` flags `ah:clog` and `ah:kb-log`; the keyboard tracker's 60 fps loop stops when no keyboard is open. (d094a918)
- **Paint.** No backdrop blur on touch devices; the status-dot glow animation is paused inside parked keep-mounted tabs. (ce185831)
- **Backend.** Responses are gzip-compressed above 1 KB; `/api/projects/folders` and `/api/v2/tasks/counts` run in the threadpool instead of blocking the event loop; `/api/files/exists-batch` runs its filesystem work in a worker thread with a 15 s result cache; `GET /api/agents` omits `context_breakdown` from list briefs; uvicorn runs with `MALLOC_ARENA_MAX=2`. (e607e8a2)
- **Models.** Opus 5.5 added and made the default; Sonnet 5 pricing and context window corrected. (83caf4da, 37b20fd1)
- **Config.** GitHub username references follow the rename to `jianpengyao`. (85744a5a)
- **Tests.** The in-memory SQLite test engine uses `StaticPool` so threadpool handlers see the same database. (e607e8a2)

## [0.17.0] - 2026-09-04

Dropbox backup release. Registered projects can be backed up to the user's Dropbox through the HTTP API v2 — no desktop client, no changes to project folders — with per-project folder selection, gitignore-style rules and event-driven uploads. Linking is one click for every install through a project-registered Dropbox app and a static return page. Attachments uploaded from the web UI now live inside the project. Also: `ultracode` as a task effort level and Opus 5 as the default for summary/insight jobs.

### Added

- **Dropbox sync, per project, off by default.** A "Dropbox Sync" row in each project's Settings card. Turning it on links the account (first time only) and opens a folder picker over the project's top-level entries: select all / deselect all, per-folder file counts and sizes streamed from a dry-run scan, "ignored" badges for `.git`/`node_modules`/symlinks, and an extra gitignore-style rules box. The engine scans incrementally (mtime+size, then Dropbox `content_hash`), uploads through upload sessions committed with `finish_batch`, resumes pending sessions after a restart (including `incorrect_offset` recovery and the 7-day session expiry), skips case-insensitive path collisions with a report, refuses torn uploads by sending `content_hash` with each commit, honours `Retry-After`, and supports an opt-in prune, a bandwidth throttle, and budgets (max file size, files per project, extension allowlist). State lives in `data/dropbox/state.db` (0600); the remote layout is `Apps/xylocopa/<project>/…`. Read-only MCP tools `dropbox_get` and `dropbox_count`. Design and API reference in `docs/dropbox-sync.md`. (05d20ada, 05e6ff2e, cc7de3a2, 579ec139, f5af50b6, 08ff4c9f, 38bfecc0, 63b6cebf, dc651ca5, 13318e08, c4f085c5, 6c166741)
- **One-click linking for every install.** OAuth 2 + PKCE with a Dropbox app key shipped in the code (a public client id — no app secret exists), and a dependency-free static return page served from this repo via GitHub Pages (`docs/oauth/dropbox/`) that remembers the instance address in the visitor's own browser, so the flow is "Continue → Allow → back in xylocopa" from any origin (localhost, LAN, Tailscale) with nothing to configure. `DROPBOX_APP_KEY` / `DROPBOX_RELAY_URL` support a bring-your-own-app setup; the paste-a-code flow stays as a fallback. Linking a project that is already enabled starts its first sync immediately. (a2f6adad, 8251ff8d, 83a099fb, 6e26bb6b, 3b0ec34f, 468c3b1a)
- **Event-driven uploads.** Nothing polls by default: an agent's Write/Edit/MultiEdit/NotebookEdit/Bash (PostToolUse hook) schedules a check 90 s later (5 min for projects over 100k files), an agent's turn ending 20 s later, an attachment upload 5 s later; enabling a project, linking and startup check immediately. Checks scan only the project involved and record a run only when something was uploaded. `interval_minutes` is an optional fallback timer (default 0 = off). Status exposes `mode`, `last_check`, `last_checked_at` and `up_to_date`. (1d9e6470, 152dfeda, a08ebb31, 5537710d, cf5bf2e2)
- **Attachments stored in the project.** Files uploaded from chat, an inbox card or the new-task page go to `<project>/.xylocopa/uploads/` (added once to the repo's `.git/info/exclude`), so agents read them without leaving the project, previews get thumbnails, and they follow the project through archive, delete and Dropbox sync. Uploads without a project keep using `UPLOADS_DIR`; the Monitor storage chart counts both. (8fb7b122, e69a5ca4, d797aa7a)
- **`ultracode` effort level** selectable when launching a task. (1331091e)

### Changed

- **Summary and insight jobs default to Opus 5.** (430c18b9)
- **Monitor page** no longer shows the Backup and Dropbox cards — backups keep running on their schedule with the routes unchanged, and Dropbox is managed from each project's settings. (97565877)
- **Dependencies:** `pathspec` added for gitignore-style matching. (05d20ada)

## [0.16.2] - 2026-09-01

Model support release: Claude Fable 5.1.

### Added

- **Claude Fable 5.1 (`claude-fable-5-1`).** Available in the model picker (leads the list; new-task default stays Opus 5) and accepted everywhere models are validated. Context tracking uses its 1M window; cost tracking carries an explicit pricing entry — $10/$50 per MTok with cache reads at $0.25/MTok, cheaper than Fable 5's $1.00, which the suffix-strip pricing fallback would otherwise have applied. Also listed in the MCP `task_create`/`task_update` model guidance. (5fac5921)

### Changed

- **Installer model prompt refreshed.** `install.js` and `.env.example` offered a stale lineup (Opus 4.8/4.7, Sonnet 4.6) with `claude-opus-4-8` as the suggested default; both now match the current picker with `claude-opus-5` as the default, in line with `DEFAULT_CLAUDE_MODEL`. (5fac5921)

## [0.16.1] - 2026-08-21

Theme system release. The light lineup is rebuilt around new palettes, the hardcoded cyan family now rides theme tokens, status colors are tuned per palette, the Monitor charts follow the active theme, and the original saturated stock look (Vivid) becomes the default light palette.

### Added

- **Light palettes: Ash, Silver, Clay.** Ash is a gray-ground/white-card look with near-black ink; Silver is a flat monotone built around a user-sampled #c2c4c7; Clay is warm greige with a cocoa-brown accent. With Nord Light and Solarized Light the light lineup is six palettes; Soft Light, Rosé Pine Dawn and Latte were dropped in the consolidation. (f363017d, aab960b2, ae24f870, 24a5f439, d8bc1177)
- **Per-theme accent and status tokens.** A global sweep replaced hardcoded cyan classes with `--color-accent` (tags, markdown links, inline code, status maps, nav chrome, chat surface, glows), and `--color-ok`/`--color-danger` let each palette carry its own green/red: Solarized uses its cyan for ok (the olive read as mustard next to the blue accent), Clay warm moss + brick, Silver grayed spruce + oxide red, Soft Dark its One-Dark terminal table. Values contrast-checked ≥3:1 against page and surface. (c8186e2e, 6390df14, 00cd48b7, d31cda1b, 8296587d, 4e73e7f9, 03a6fbf4)
- **Theme-aware Monitor charts.** Storage-donut segments read the same tokens as their legend dots (the ring previously hardcoded bright Tailwind hexes — on Clay it drew cyan next to a cocoa legend dot), categorical hues are muted per palette via `--chart-mute`, and the usage bars' warning states ride the attn/danger tokens. (6ee2126e)

### Changed

- **Vivid is the stock light palette; Light retired.** The pre-token stock look (cyan-500 accent and fills, cyan-600 bubble, green-500/red-500 status) now lives directly in `:root`, verified hex-for-hex against the pre-token commit. The retired Light preset's AA-tuned green/red live on in Ash; stored `vivid`/`light` selections keep rendering identically. (91a83f9f, 7eb2661e, 85bccb87)
- **Docs.** Getting-started rewritten against the current UI (en + zh, unified structure); README restructured into flowing prose. (48cebf52, 2a3965b1, d00bb1c7)

### Fixed

- **Tag chips and links rendered unstyled** — the accent sweep referenced `.text-accent`/`.accent-tint-15` utilities that were never defined, so chips lost their background and text color. (29bcd5a9)
- **Solarized Dark dim/hint text below 3:1** — lifted to 3.2:1, the one sub-3:1 text tier in the lineup. (05b63b00)

## [0.16.0] - 2026-08-21

Assistant character release (experimental). The UI layer for the attention job engine that shipped in v0.15.1: an orb assistant with an animated face, a speech-bubble chat that turns natural-language requests into attention jobs, and a character-skin system. Off by default — enabled via an Experimental toggle in Monitor > Display.

### Added

- **Assistant character (experimental, off by default).** An orb FAB with an animated emoji-style face (Fluent/Noto-family styling): pointer-tracking pupils, a celebration when a job fires, and rect-based parking that keeps out of the composer and nav. Tapping toggles its speech bubble — the FAB never navigates. The bubble chat creates attention jobs (reminders, state watchers, schedules) through the backend compiler, shows inline job proposals with previews, and manages existing jobs. Skins: preset faces plus Opus-generated custom characters (new chargen endpoint). (e6cb58bf)
- **Kill switch.** With the toggle off, the corner button is the classic unread FAB — cyan count badge, tap opens the oldest unread chat, long-press opens split screen — and the bottom-nav unread badge stays classic red. The toggle swaps the two subtrees live.

## [0.15.2] - 2026-08-21

Unread-badge reliability fix.

### Fixed

- **Unread badges could silently diverge from the agent cards.** The unread FAB and bottom-nav badge read a WebSocket-only map; if the socket died without reconnecting, badges froze at a stale total while the agent cards (5s HTTP polls) kept showing fresh counts. The badge total is now derived from the same list payload the cards render — every agents fetch feeds the shared unread state — with a visibilitychange resync covering the laptop-sleep case on routes with no list mounted. Event-driven only, no new polling. (5be01a32)

## [0.15.1] - 2026-08-21

Backend-only release: the attention job engine, a pluggable (trigger x action) automation layer for the orchestrator. No UI ships in this release — jobs are managed through the REST API (`/api/attention/*`), including a conversational endpoint that compiles natural-language requests into jobs. The assistant frontend built on top of this engine is being polished separately.

### Added

- **Attention job engine.** One `AttentionJob` row = one trigger x one action, both resolved through pluggable registries. Triggers: `at` (one-shot), `every` (interval, rate-floored), `signal` (conditions over named app-state signals, with `became` edge semantics and per-job cooldown so state watchers fire on transitions instead of spamming), `probe` (webhook). Actions: `notify` (web push), `message_agent`, `dispatch_task`, `run_prompt`. Evaluation rides the existing 2s dispatcher tick and delivery reuses the app's existing paths (notify.py, the probe wake path, task_service) — no new scheduling machinery. `daily_at` schedules are local wall-clock with an optional weekdays filter, and previews are backed by the real trigger arithmetic. A `/chat` endpoint compiles natural language into job definitions via `claude -p`; job firings broadcast an `attention_fired` WebSocket event. Ships with a 59-test suite. (0e6d1d24, b57537f7, 3f5f119e, bdc024d6)

## [0.15.0] - 2026-08-21

Theme release. The app gains a theme system: five preset palettes beyond the stock Light/Dark, a custom theme editor, per-palette user-bubble colors following messenger conventions, and a terminal overlay that follows the active theme. Also a set of smaller fixes: UTF-8-pinned telemetry file IO, WebSocket forwarding for port-preview cards, keyboard-focus delegation for sandboxed preview iframes, log rotation for the frontend/kb debug logs, and an mcp<2 dependency pin.

### Added

- **Theme palettes.** Soft Dark, Solarized Light, Solarized Dark, Nord, and Everforest join the stock Light/Dark. Each palette is a `theme-<id>` token-override class layered over its light/dark base, so every existing `dark:` Tailwind variant keeps working; glass surfaces, e-ink mode, and reduced-transparency fallbacks retain their existing precedence. The picker lives in Monitor > Display as swatch cards; picking a palette also switches to its base, and the sun/moon header button flips between the palettes chosen for light and dark. The `index.html` init script restores the palette class pre-paint. (2d67946b, 0a25910d, 113c8863)
- **Custom theme editor.** Six core colors (background, card, heading text, body text, border, user bubble) plus a light/dark base; the remaining ~20 tokens derive automatically, compile to CSS, and inject ahead of the bundled stylesheet. The editor mirrors whichever palette is selected — with a preset active it shows that preset's colors, and the first edit forks them into the Custom palette. (b065453a, 0f253635)
- **Per-palette user chat bubble.** Bubble colors are tokenized (`--color-bubble/-ink/-dim`) and follow messenger conventions: light themes keep a saturated blue bubble with white ink; dark themes use a deep desaturated hue with off-white ink pulled toward each theme's body color. Source chips in the shared timestamp row now branch on message side, fixing agent-side chip readability in light mode. (7b486d20, 2c9b4088, d71cefdb, 3a336b0e, 2c1afc04)
- **Themed terminal.** The attached-terminal overlay resolves its xterm.js theme from the active palette — canonical ANSI-16 tables for Solarized, Nord, and Everforest, GitHub Light/Dark for the stock palettes, derived colors for custom themes — and hot-swaps it if the theme changes while open. The overlay chrome (header, reconnect banner, mobile key bar) uses theme tokens instead of hardcoded dark colors. (f7590bb6)

### Fixed

- **Telemetry file IO pinned to UTF-8.** CJK-locale Windows read `package.json`/`config.yaml` with the locale codec, reporting version "unknown" and silently ignoring the config opt-out. (de2c548e)
- **Port-preview WebSockets.** The frontend dev/preview proxy forwards `/api` with `ws: true`, so port-preview cards' WebSockets connect. (fc9983ee)
- **Sandboxed preview iframes** get keyboard focus delegated, so typing works without tapping inside first. (77b3bba6)
- **Debug log rotation.** `frontend-debug` and `kb-debug` logs size-rotate (20 MB x 3) and are covered by the truncate endpoint; the frontend debug logger no longer propagates to the root logger. (0548246c, 49f4a86f)
- **mcp pinned below 2.0** — SDK 2.0 removed `mcp.server.fastmcp`, breaking MCP server startup. (dc192b48)

### Changed

- **Git identity migration.** Managed repos with the legacy "AgentHive" committer identity migrate to "Xylocopa" on startup; `GIT_USER_NAME` env documented. (20f0861a, 367dfd55)

### Fixed

- **Agents still committing as `AgentHive`.** Pre-rebrand orchestrators wrote `user.name=AgentHive` / `user.email=agenthive@localhost` into each managed repo's `.git/config` on merge; the rebrand changed the code but not configs already on disk, so agents (and their worktrees, which share the parent's config) kept authoring commits as AgentHive. Startup now rewrites that exact legacy identity in every DB-known project repo to `Xylocopa <xylocopa@localhost>` (idempotent; user-set identities untouched). Author name is now overridable via `GIT_USER_NAME`, alongside the existing `GIT_USER_EMAIL`.

## [0.14.0] - 2026-07-24

Conversation branching release. A chat message can now be forked into a new agent that inherits the full conversation — including tool inputs/outputs and file-state context — by truncating and resuming the underlying Claude Code session transcript. Also fixes the localStorage quota exhaustion that silently dropped chat drafts and voice transcripts on long-lived installs.

### Added

- **Diverge conversations.** Double-tap a chat bubble → the action menu gains a split-arrows button that forks the conversation at that message into a new agent. The source session JSONL is truncated at the message, re-keyed to a fresh session id, and resumed in a new tmux pane, so the branch replays the original transcript verbatim. On an agent reply the branch keeps that reply and continues after it; on a user message the branch cuts before it and the text prefills the new chat's composer for edit-and-resend. An optional purpose becomes the new agent's name, the Task title (panel attributes inherit from the source task), and the composer prefill. Pre-compact/clear sessions are searched too, so old messages remain forkable. Forked transcripts close with a standard CC interrupt marker, so agent status converges to IDLE through the normal JSONL-derived state machine with no out-of-band writes. (f77eb067, ce487d4e, 53e23579, 9f73fb31, e86f867c, cd7905e0)

### Fixed

- **localStorage quota exhaustion ("The quota has been exceeded").** The persisted detail caches (agent briefs, insight suggestions) grew one entry per agent ever seen and eventually filled the origin quota — from then on chat drafts, voice transcripts, and composer prefills were silently dropped. Persisted caches are now capped at the 80 newest entries, and draft/voice/prefill writes go through a quota-safe helper that evicts advisory caches (largest first) and retries; real drafts and auth state are never evicted. (22862189)

## [0.13.0] - 2026-07-24

Model refresh release. Claude Opus 5 (released by Anthropic this month at the same $5/$25 pricing as Opus 4.8) becomes the default model for new tasks and projects, the model picker is slimmed to current tiers, the effort picker regains the missing High level, and the background summarization prompts are retuned. Also restores the frosted-glass treatment on the nav and input bars that never rendered on Chromium.

### Added

- **Claude Opus 5 support** (`claude-opus-5`). Wired through the full stack: model picker, backend `VALID_MODELS` validation, pricing table ($5/$25 per MTok), 1M context-window limit for the context pill, and the MCP `task_create` model-selection guidance. (62270a45, 632a66e2)

### Changed

- **Default model is now Opus 5** for new tasks and projects (`DEFAULT_MODEL` / `DEFAULT_CLAUDE_MODEL`). Fable 5 still leads the picker visually but stays opt-in due to ~2x cost. Tests now assert the config constant instead of a hardcoded model id. (289d1a4d)
- **Model picker slimmed to five options** — Fable 5, Opus 5, Opus 4.6, Sonnet 5, Haiku 4.5. Opus 4.7 and 4.8 are removed from the picker but remain valid on the backend, so existing tasks/projects keep working and their tags keep proper labels via a legacy-label map. (b69ea27d, 6804a79d)
- **Effort pickers include High again.** All four selectors (EffortSelector, NewTaskPage, QueueCard, InboxCard) offered only Low/Medium/XHigh/Max; the backend always accepted `high`. (62270a45)
- **Summary prompts retuned.** Agent-insight extraction is tighter (max 8 items, one sentence each, drop anything obvious from the code); retry summaries are richer (5-10 bullets, ~1500 chars, with files touched, concrete failure evidence, and ruled-out dead ends) since they are the next attempt's only window into the previous one. (6ea85179)
- **OpenAI helper calls read `OPENAI_QUICK_MODEL`** (default `gpt-4o-mini`) instead of overloading `SUMMARY_MODEL`, which now exclusively selects the Claude model for `claude -p` background jobs. (a38b5b9c)

### Fixed

- **Frosted glass on nav + input bars renders on Chromium.** The `backdrop-filter` treatment silently never worked; fixed the root cause, tuned glass alpha to .65 light / .68 dark, and added a standalone `/glass-diag.html` device diagnostic. (aab41a04, 579637e8, a8c89e3b)

## [0.12.1] - 2026-07-18

Patch release hardening the v0.12.0 web terminal on mobile and fixing a chat-sync stall introduced by Claude Code's daemon-side auto-compact.

### Fixed

- **Chat sync survives daemon-side auto-compact.** Claude Code's daemon can compact a managed agent's conversation by forking it onto a new session id (`--fork-session --resume <current jsonl>`) with no agent header in the SessionStart hook; the orchestrator kept tailing the frozen pre-compact JSONL and the agent's chat silently stopped updating. The internal-worker classifier now surfaces the lineage `XY_AGENT_ID` and the resumed session parsed from argv, and when a fork resumes exactly an agent's current session the hook performs the same rotation as the in-process compact path and wakes sync. Verified live: replaying the fork hook rotated the stuck agent and imported the 60 missed messages. (321db643)
- **Terminal overlay auto-reconnects.** iOS suspends the WebSocket whenever the PWA is backgrounded, which previously left a stale frozen screen and a manual Reconnect button. Returning to the app now re-attaches immediately (visibilitychange), foreground drops retry up to 3× with backoff, and a 25 s ping keeps NAT/proxy idle timeouts away. Clean exits and errors still require manual reconnect. (5edcbbca)
- **No app bleed-through when the soft keyboard opens.** iOS scrolls the page to keep the focused input visible, so shrinking the overlay to `visualViewport.height` alone exposed a strip of the underlying chat page above the keyboard. The terminal pane now pins to the visual viewport (height + `translateY(offsetTop)`, tracking both resize and scroll) inside a full-screen backdrop that never resizes. (23b001d8)

## [0.12.0] - 2026-07-18

Interactive web terminal release. The chat header gains a terminal icon that attaches straight to the agent's live tmux session from any browser — phone included — over a new WebSocket PTY bridge. Also fixes CLI-session discovery misclassifying Claude Code's internal daemon workers as adoptable sessions.

### Added

- **Web terminal (tmux attach) in chat.** A terminal icon in the chat header opens a full-screen xterm.js overlay attached to the agent's tmux session via the new `/ws/terminal/{agent_id}` endpoint: a server-side PTY runs a dedicated `tmux attach` client, binary frames carry PTY output, JSON messages carry input/resize/ping. Closing the overlay only detaches that client — the session keeps running. `window-size latest` is set on attach so a phone-sized client doesn't shrink the desktop tmux view. Touch devices get an Esc/Tab/sticky-Ctrl/arrows key bar; visualViewport handling keeps the prompt above the soft keyboard; xterm.js loads lazily (~300 KB chunk) only when the terminal is opened; the overlay respects the iOS safe-area top inset. (87350f55, 905e5ef5, fbbe190a)

### Fixed

- **CLI-session discovery no longer flags Claude-internal workers.** Claude Code's daemon architecture (bg-pty hosts, `--fork-session` compact workers, subagents) runs pane-less claude processes whose SessionStart hooks look like a user CLI started outside tmux; they surfaced as adoptable "not in tmux" entries, and adopting one would kill a live internal worker and resume its session in a spurious tmux session. The hook now walks /proc lineage of the process owning the session id (exact argv-token match against a claude binary) — XY_AGENT_ID environment or daemon/bg-pty-host ancestry marks the session internal and skips the entry; `source="fork"` is never adoptable. (f670641d)

### Changed

- **Queued/scheduled message editing moved to the composer.** "Modify" on a queued or scheduled bubble loads the text into the bottom input bar instead of an inline bubble textarea, so the text stays visible above the mobile keyboard while editing. (29fa52f1)

## [0.11.0] - 2026-06-10

Interactive web-app preview release. Agents can now hand the user runnable deliverables — data explorers, 3D viewers, local dashboards — as tappable cards in chat, opening fullscreen in a sandboxed iframe with a built-in console drawer. Verified end-to-end with TensorBoard through the port proxy and an agent-built three.js exoplanet viewer on iPhone.

### Added

- **Web-app cards in chat.** New `webapp_present` / `webapp_list` MCP tools post `kind="webapp"` card messages and maintain a per-project registry (new `webapps` table) so later agents can find and reuse existing viewers. Cards are tool-driven only; plain-text `.html` paths no longer render run cards. (6c868bb1, bb5433e5)
- **Static app serving.** `/api/preview/t/{token}/{project}/{path}` serves project files for preview with a path-embedded, preview-scoped 12 h token, so relative subresources (`./app.js`, `./data.json`) authenticate without cookies. Responses carry a CSP `sandbox` header and `Access-Control-Allow-Origin: *`; served HTML gets an injected script that mirrors `console.*` and uncaught errors to the panel's debug drawer. (35fa749f)
- **Localhost port proxy.** `/api/preview/p/{sig}/{project}/{port}/{path}` reverse-proxies registered local services (TensorBoard, dev servers) with a stable HMAC capability prefix, WebSocket relay with subprotocol passthrough, a registered-port allowlist, and the orchestrator's own port denied. Cookie-session apps (wandb local, jupyter) are documented as `url` cards instead — the sandbox is credential-less by design. (6c868bb1, 41bc3c08)
- **Sandbox compatibility shims.** Injected into served/proxied HTML: in-memory `localStorage`/`sessionStorage` and no-op `document.cookie` (opaque-origin access throws otherwise), and a `Worker` wrapper that falls back to sync-XHR + blob URL when URL construction is blocked — required for TensorBoard's worker-based chart renderer. (bd2f85cb)
- README feature entry with a phone screenshot of the preview panel. (6763dbb5)

### Fixed

- Status signals only emit on genuinely-new turns, preventing re-fired side effects on replayed history. (3adba714)
- Drift audits exclude intentionally-unimported turns, removing false-positive purge warnings. (89c9e1d6)

### Changed

- **Auth: scoped tokens are rejected as session tokens.** `verify_token` now refuses any token carrying a `scope` claim, so a leaked preview URL can never grant API access; `decode_token` returns the verified payload for endpoint-specific checks. (35fa749f)
- **CORS: the literal `null` origin is allowed** (sandboxed iframes send it) with `allow_credentials=False`, and vite's own CORS middleware is disabled so `/api` preflights reach the backend instead of being answered with a rejecting allowlist. (bd2f85cb)
- Documentation sweep: README rewritten (351 → 213 lines, one home per fact), getting-started (en/zh) tightened, `agent-mcp-tools.md` updated with the probe and webapp domains, stale MCP tool list in ARCHITECTURE.md corrected. (20231530, 745faa4b)
- Orchestrator refactors: lookup-or-404 helpers replace 45 boilerplate sites, shared `build_task` extracted to `task_service`, PORT/default-model constants centralized, `_utcnow` deduplicated, dead code removed. (d1b8d59f, 02d5e4c9, 8edd9d36, 9a861d5b, 1ea9fc75)

## [0.10.15] - 2026-05-14

Memory-leak hunt release. Two OOMs on May 13 grew orchestrator RSS to 47.8 GB before the kernel killed the process — which took every tmux'd xylo session down with it. This release plugs five leak vectors found via a parallel multi-agent code audit, adds a `max_memory_restart` safety net so the next leak triggers a pm2 graceful-restart at 8 GB instead of a kernel global-OOM, and ships diagnostic infrastructure (size-bounded forensic log channels, a live `/api/debug/mem-introspect` endpoint, a header-pill memory-pressure indicator) so the next leak leaves a usable trail.

### Memory leak fixes

- **`_promote_ack_pending` / `_promote_ack_by_agent` never cleaned in `stop_agent_cleanup` / `error_agent_cleanup`.** The GHOST_DELIVERED instrumentation dict (added 2026-04-30) records ~500B–1KB per promoted message and was explicitly designed to "leave it in the dict so a late hook can still ack it" — but stopping or erroring an agent didn't drop those records. Across many sent messages plus agent churn this accumulated unboundedly. `stop_agent_cleanup` and `error_agent_cleanup` now pop from both dicts, and `_record_promote_for_ack` caps the per-agent queue at 256 entries so even long-running agents that never stop can't grow it forever. While in there, also fixed missing cleanup of `_generation_ids` and `_known_subagents`. (822edfa, b96870b)

- **`cc_session_reconcile.reconcile_all` shared one SQLAlchemy session across 500+ agents without flushing the identity map.** Each agent's `reconcile_agent` loads CCSession ORM rows into the shared session; at the 80k-JSONL scale the identity map accumulated tens of thousands of ORM objects pinned until the final commit. Now flushes pending writes and runs `db.expunge_all()` after each agent so memory tracks one agent at a time. Reconcile time grew from a few seconds to ~9 minutes for the 92k-JSONL scan, but RSS stays bounded. (822edfa)

- **`claude -p` insight/summary subprocesses had no concurrency cap.** Six call sites in `routers/{agents,projects,tasks}.py` spawned `threading.Thread(daemon=True).start()` per request. When many agents stopped at once each call buffered `proc.communicate()` stdout in memory, and concurrent runs stacked GBs — the ~10 GB sibling python process seen alongside the killed orchestrator at both OOMs. Replaced all six sites with `INSIGHT_EXECUTOR = ThreadPoolExecutor(max_workers=2)` so at most two claude subprocesses run concurrently and the rest queue. (822edfa)

- **`display_writer._pre_sent_index` not cleared when an agent was deleted.** Per-agent pre-sent message state stayed in the module dict indefinitely for deleted/permanently-removed agents. `delete_agent()` now pops the entry and discards the agent_id from `_pre_sent_index_ready`. (b96870b)

### Safety net

- **pm2 `max_memory_restart: '8G'`.** Steady-state RSS is ~200 MB so 8 GB is well above any normal spike. If a future leak passes that threshold pm2 graceful-restarts the backend instead of letting it grow until the kernel global-OOMs the whole user.slice (which previously took every tmux session down with it). Needs `pm-logrotate` module — installed and configured (50 MB × 4 retained = 200 MB per pm2 log stream). (5d66db3)

- **Leak-alert probe wakes the diagnostic chat at 5 GB.** RSS_WATCH posts to `XY_RSS_LEAK_PROBE_URL` (in `.env`, gitignored) once when RSS first crosses 5 GB so the diagnostic chat gets woken in real time, ~3 GB of headroom before pm2's restart kicks in. Single-fire by design; renew via the `probe_create` MCP tool. (c248047)

### Diagnostic infrastructure

- **`/api/debug/mem-introspect` endpoint.** Returns `/proc/self/status` (VmRSS/Peak/Anon/File/Threads), `gc.get_count/stats/total_objects`, top 20 object types by count, and sizes of every named in-memory cache we own (`_promote_ack_pending`, `_pre_sent_index`, `_translate_cache`, `_INSIGHT_RUNS`, etc.). `?collect=1` runs `gc.collect()` + `libc.malloc_trim(0)` first so the breakdown reflects retained memory not pending-sweep. Auth-exempt for shell access during an incident. (3b33133)

- **Four forensic log channels (internal-only, never reach the UI).** All write to `orchestrator.log` so size-bounded log rotation contains them:
  - `RSS_WATCH baseline` every 5 min logs the full memory shape (`rss/vm/peak/anon/file MB`) so the next leak leaves a curve to look back at — not just threshold crossings. Plus `RSS_WATCH: crossed N MB` on first cross of each threshold (200/500/1k/2k/5k/10k/20k MB) and `RSS_WATCH: jumped N MB in last minute` if growth exceeds 200 MB/min. (6949c9f)
  - `cc_session reconcile … took=Xs rss=A→BMB delta=+CMB` every reconcile cycle. (65823d8)
  - `claude_subproc <label> rc=X took=Ys stdout=NB stderr=MB` for every `claude -p` subprocess (claudemd_refresh / progress_summary / agent_insight / retry_summary). (65823d8)
  - `HOOK_HTTP_IN body=NKB` on every hook, plus a loud `HOOK_BIG_BODY` warning if any single hook posts >1 MB. (65823d8)

### UI surface

- **Header pill turns amber/red on memory pressure.** Previously only reflected `/api/health`, so it stayed green right up until pm2 killed the backend. Now also reads `sysStats.xylocopa.mem_mb` (orchestrator backend + child subprocess RSS, polled every 60s by `MonitorContext`): `>1 GB` → amber `Mem 1.2 GB`, `>5 GB` → red pulsing `Mem 6.4 GB`. Tooltip shows the exact number. Both `PageHeader` and the in-chat pill in `AgentChatPage` updated. (597677d)

### Log size + tail-read

- **Size-bounded log rotation.** `log_config` switched from `TimedRotatingFileHandler` (daily, 7 backups, no size cap — chatty days hit 50 MB+) to `RotatingFileHandler` (50 MB × 4 backups = ~200 MB per stream). `pm2-logrotate` module installed with the same shape so `backend-pm2.log` / `backend-pm2-error.log` can no longer balloon to 450 MB. One-time cleanup of stale logs (`kb-debug.log`, `server.log`, pre-rotation pm2 backups): `logs/` went from 1.2 GB to 139 MB. (19e12a2)

- **`get_recent_logs()` tail-reads.** Was `f.readlines()` of the whole orchestrator.log on every `/api/logs` poll — 50 MB allocated per request. Now seeks to the last 2 MB and decodes only that window. (19e12a2)

## [0.10.14] - 2026-05-12

Frontend polish release: markdown tables get a fullscreen preview, mobile-landscape regression on the code-copy + table-expand buttons is fixed, and native text selection inside expanded inbox cards now survives clicks.

### Frontend

- **Markdown tables: expand-to-lightbox button.** A small `Maximize2` chip top-right of every rendered table (mirroring the code-block copy button) opens a centered lightbox showing the full table at larger padding / text-sm via `createPortal`. Backdrop click and Escape both dismiss. Styled with app theme tokens (`bg-surface` / `bg-page` / `border-divider` / `text-label`) rather than the media-viewer's white-on-black, so it reads as an app modal in both light and dark themes. (30ecd6a)

- **Code-copy and table-expand buttons now show on touch devices in landscape.** The Tailwind `sm:opacity-0 sm:group-hover:opacity-100` pattern keyed visibility off viewport width (≥640px = hover-to-show). Phones in landscape passed that threshold and fell into the desktop branch, but touchscreens don't fire `:hover`, so the buttons stayed at opacity 0. Switched to `[@media(hover:hover)]:` so the hover-to-show behavior only applies to mouse-capable devices; touch devices (in either orientation, regardless of width) always show the button. (bd052f8)

- **Expanded inbox card: native text selection preserved.** Title and description `onClick` handlers called `placeCaretAtPoint` unconditionally, which collapsed any selection produced by drag, double-click (word), or triple-click (line). Now early-return when `e.detail > 1` or `getSelection().isCollapsed === false`, so native selection survives. Collapsed-state CSS `user-select: none` is intentional and unchanged. (665271e)

## [0.10.13] - 2026-05-12

Hook→sync flush race fix. The fixed 150ms post-hook sleep that preceded every `wake_sync` was racing with CC's JSONL flush — when the buffer flushed slower than 150ms, the wake saw "file unchanged", bailed, and the next attempt was up to 5 minutes away (POLL_INTERVAL). Result: visible message lag on a small but real fraction of hooks. Replaced with a two-phase wait that holds out for the actual content to land.

### Message sync

- **Two-phase JSONL flush wait.** `_await_jsonl_flush` now runs Phase 1 (fixed 150ms sleep, then size check vs the file size at hook arrival) and, only if file didn't grow, falls through to Phase 2 (watchdog listener for the next modify event, up to ~9.85s remaining budget). Total timeout 10s. Replaces the old `await asyncio.sleep(JSONL_FLUSH_DELAY)` pattern at all 8 hook sites (`session_end`, `user_prompt`, `stop`, `post_compact`, `tool_activity` PreToolUse + PreCompact, `session_start` /clear + managed).

- **Baseline is hook-arrival file size, not `last_offset`.** Unrelated housekeeping writes between the last sync and the current hook (e.g. earlier PreToolUse attachment entries — these alone account for ~36% of all JSONL entries) would inflate a `last_offset`-based check, causing a false-positive "flush observed" that misses the actual content the hook is signaling. Comparing against the size snapshot at function entry restricts the check to writes that happened *after* the hook fired.

- **Phase-outcome logging.** Each `_await_jsonl_flush` invocation now logs which phase caught the flush, elapsed time, and bytes grown. Phase 2 timeouts log at WARNING so slow CC flushes are surfaced.

### Observed impact

Compared 2572 hooks across 7 days of pre-fix logs vs 44 post-fix hooks:

| | OLD code (7 days) | NEW code (44 samples) |
|---|---:|---:|
| Hooks delayed >1s | 2.64% | 0% |
| Hooks delayed >5s | 2.29% | 0% |
| Hooks delayed >30s | 0.39% | 0% |
| Hooks delayed >2min | 0.12% | 0% |
| p99 hook→sync delay | 16,651ms | 593ms |
| max observed | 271,594ms (4.5min) | 593ms |

Visible-lag and stuck-until-manual-refresh message syncs are essentially gone in observed samples. Post-fix p99 sits at ~0.6s; remaining tail above that would require a CC flush >10s, which has not been observed.

## [0.10.12] - 2026-05-12

Interactive-card pipeline cleanup. Three coupled changes turn the permission / AskUserQuestion / ExitPlanMode handling into a flatter design where JSONL is the single source of truth for what Claude actually received, the unworkable `Notification(permission_prompt)` → ghost-card path is removed, and UI-vs-JSONL disagreements surface as a side-channel SystemBubble rather than silently overwriting card metadata.

### Interactive cards

- **Ghost permission cards removed.** xylocopa previously subscribed to the CC `Notification(permission_prompt)` hook in an attempt to catch native permission prompts that bubble up from subagents running under `--dangerously-skip-permissions`. The Notification payload doesn't carry `tool_name` or `request_id`, so the rescue produced unanswerable cards labelled `tool_name="unknown"` and never actually surfaced live subagent permissions. Database scan shows 5 such cards in the prior 6 weeks, none corresponding to an active subagent permission request. The subscription, handler, and subprocess-filter exception are gone; the real permission gateway (PreToolUse + PermissionManager) is untouched.

- **JSONL is authoritative for interactive answers.** The merge step that previously kept a DB-side answer when JSONL recorded a dismiss (`User declined` / `Tool use rejected` / `The user doesn't want to proceed`) is removed. When JSONL has a non-null answer it now always wins; the DB still contributes `selected_index` / `selected_indices` since CC's tool_result doesn't carry numeric indices.

- **Multi-question AskUserQuestion fallback dismisses the picker.** When `PermissionManager` has no live hook request (e.g. orchestrator restarted between question and answer), the tmux-keys fallback used to replay `Down × N + Enter` for each question, but multi-question pickers need per-question navigation that can't be reconstructed from the final question's request — the previous code corrupted earlier answers and CC wrote `User declined`. Single-Q fallback is unchanged. Multi-Q fallback now sends `Escape` to dismiss the picker and returns 503 so the user can re-answer in TUI. ExitPlanMode is untouched — its single Enter is reliable.

- **UI / JSONL answer mismatch surfaces as a SystemBubble.** A read-only sweep at the end of each sync compares each interactive item's JSONL answer against the DB's stored answer. When JSONL is a dismiss-pattern and the DB has a real UI selection, a SystemBubble is emitted: `Selection didn't reach Claude (got "...")`. The card's metadata is not mutated — the card continues to render the user's selection, the sys bubble below records what Claude actually received. Dedup is per `tool_use_id`.

### Backend

- tmux launch path now does a preflight check and logs `resume_agent` exceptions explicitly.

## [0.10.11] - 2026-05-11

Cert management on `/cert-guide` is now idempotent and the PWA service worker no longer caches `index.html`, eliminating the failure modes that surface as "Load failed" / "Reconnecting to server..." after a cert regeneration. Existing PWAs auto-clear their caches on first load (CV `v3` → `v4`); no manual action required for the upgrade itself.

### ⚠️ If anything looks off after upgrade

Clear your browser's site data for the host once (Settings → Privacy → site data → Remove). **Your login state and preferences are preserved** — the cleanup only touches the Service Worker registration and the Cache API (HTTP response cache). `localStorage` (auth token, route memory, theme), `sessionStorage`, and `indexedDB` are untouched.

This is the same one-shot cleanup the new CV `v3` → `v4` check performs automatically; doing it manually is only needed if the auto-trigger didn't fire (e.g. the tab never reloaded after the SW upgrade).

### Certificate flow

- `/cert-guide` has a new "Step 1: Regenerate Certificate" section. The user confirms the IP the browser is currently using (auto-filled from `window.location.hostname`) and clicks Regenerate. The backend signs a fresh leaf with mkcert directly.
- Regeneration is idempotent: if the current cert's SAN matches the requested set exactly, the regeneration is skipped (`{skipped: true}`). The UI shows a green "Already in cert" badge and the button switches to a disabled "No regen needed" state in that case.
- Empty-body `POST /api/cert/regenerate` now returns 400 instead of falling back to `hostname -I` auto-detection (which silently missed tailnet-shared IPs and locked those clients out).
- Added `GET /api/cert/info` returning just the current SAN (IPs + DNS) — used by cert-guide to drive the coverage badge.
- Removed `tools/ensure-cert.sh` and its `openssl` fallback (the fallback would silently rotate the CA, a worse failure mode than failing loudly on missing `mkcert`).

### Service worker

- `registerType` switched from `'prompt'` to `'autoUpdate'`. Paired with `skipWaiting: true` and the newly-added `clientsClaim: true`, this resolves the previous contradictory configuration where new SWs activated immediately but never claimed existing tabs.
- Dropped `html` from precache `globPatterns` and set `navigateFallback: null`. Navigation requests now flow through a `NetworkOnly` runtime route, so TLS / cert failures surface as the browser's own warning page instead of being masked by SW-served stale HTML.
- Bumped cache version `CV` from `v3` to `v4` — old installs auto-unregister all SWs, clear all caches, and reload once on next load.
- Added self-heal script in `index.html` (capture-phase listener for `/assets/*.{js,css,mjs}` load failures → SW unregister + cache clear + reload, session-storage gated to prevent loops).

### cert-guide layout

- Switched the page container from `flex items-center justify-center overflow-y-auto` (well-known flexbox-scroll bug that clipped tall content) to top-aligned scroll with a fixed backdrop.
- Added `paddingTop: max(2rem, env(safe-area-inset-top))` and matching bottom padding — iOS Dynamic Island / notch and home indicator no longer overlap content.

### Frontend (unrelated to cert/SW)

- Glass surfaces default to opaque (drop `backdrop-filter`); `.glass-bar-nav` retains the semi-transparent treatment.
- Scroll-to-bottom FAB rendered opaque.
- formatters: `.tex` and `.bib` files recognized as agent attachments.

### Documentation

- Probe feature documented in README; "What's New" link added to top navigation.

### Upgrade notes

PWA caches are invalidated automatically on first load after the upgrade. No manual action is required for the codebase upgrade itself.

If your self-signed cert was regenerated during the upgrade window and the mkcert CA root is not installed on your client device, you may need to re-accept the cert once via Safari's URL bar warning page. This is unrelated to the release.

## [0.10.10] - 2026-05-10

A focused release covering the new probe → chat wake-up flow, interactive-card surface alignment (preview, push body, unread accounting), and a handful of polish fixes for defer expiry, insights caching, and the git tab.

### Features

- **Probes** wake a target chat when an external webhook fires. Probes share `source=web` and are identified by `metadata.probe_id`. They auto-expire when the agent transitions to STOPPED/ERROR, drop their dedicated notify channel in favor of the standard chat path, and the envelope contract has been hardened against a sync whitelist regression. Pre-sent dedup now includes `source=probe` so duplicate webhook fires no longer race the dispatcher. (0e48b67, c858555, 0f1d7f3, 7b11937, 81734b1, cd826ac)

### Interactive cards

- AskUserQuestion / ExitPlanMode / Permission cards now bump `unread_count` to mirror stop-hook behavior — previously these blocking cards left the agent at zero unread despite waiting on user input. Native-permission unread bump is scoped inside the try block so a DB write failure no longer fires a stray push without a persisted card. (c938774, de33a6a)
- Last-message preview no longer falls through to "No messages yet" when the latest turn is an interactive card with empty content — synthetic preview now extracts the question / plan text from metadata. (675e4c8)
- Preview tag and push body unified across all three card types as `[interactive cards] {content}` so the inbox row, push notification, and chat list stay aligned. (4e9db7b)
- Removed the 10-min stale-EXECUTING fallback that flipped agents to IDLE while a long AskUserQuestion or ExitPlanMode was still pending — JSONL is now the sole source of truth for IDLE / EXECUTING. (e3b46c8)

### Defer

- `deferred_to` clears on expiry via the dispatcher tick instead of requiring a manual sweep at message dispatch time. (f9587ae)
- Defer-sweep coalesces N `task_update` broadcasts into a single `tasks_invalidated` event, cutting the WebSocket fan-out when many tasks expire in the same tick. (f14149d)

### Frontend polish

- Insights: persist `briefCache`, history cache, and pending suggestions to remove the entry flicker and ProgressSuggestionsCard delay. (d499209, 6f9a97c)
- SendLaterPicker: default to today + current time on open; minute step now ±1; value cells are directly editable. (2dd8ffc, 3b4e414)
- `notify_at` icon unified to `lucide:Bell` across all surfaces. (b5a0fc0)
- Git tab: auto-refresh on tab activate plus an explicit manual refresh button. (5fa5c64)
- Refresh icon spin direction now matches the arrow direction. (f092c27)
- E-ink mode: restore saturated highlight on popover primary buttons that lost it in a prior pass. (5fc5fd3)

## [0.10.9] - 2026-05-08

### Fixed

- **Agents page no longer shows stale unread / message preview for 5 s after returning from background.** `AgentsPage.jsx` activate effect was setting up a `setInterval(POLL_INTERVAL=5000)` without an immediate first call, so resuming the PWA from background (or switching to the Agents tab from another tab) left the list rendering whatever data was last fetched before the page went idle — typically up to 5 s out of date — even though the push notification had already arrived and the DB had the new state. The mount-time seed (`seededRef`) didn't cover this path because it persists across activations. Activate now fires `pollTick()` and `loadUnlinked()` immediately before installing the interval, dropping the worst-case stale window from ~5 s to one network round-trip (~50 ms). MonitorContext's separate 2 s warm-up `fetchAgents` doesn't help here because it writes to its own state (used by MonitorPage), not the AgentsContext store.

## [0.10.8] - 2026-05-08

Maintenance release. Two real bugs surfaced from chasing visual glitches in the interactive cards: a static `bg-hover` utility was missing (so the "selected" fill on Question / Permission / Plan cards was transparent for many iterations), and Tailwind v4 doesn't generate alpha modifiers for `@layer utilities` custom classes — so `border-divider/60`, `text-dim/50`, `border-edge/40` etc. silently fell back to `currentColor`, which is what the dimmed borders looked "near-black". Backend side: the 150ms timer that woke the agent dispatcher after JSONL flush was replaced with a watchdog-driven event, and PostCompact status flips now stop lying via the rotation path. Other touches across lightbox/media handling, cert-install flow, scheduled-message editing, and insight bubble plumbing.

### Interactive cards

- Watchdog-driven JSONL flush wake replaces the fixed 150ms `_delayed_interactive_wake`; agent dispatcher and `routers/hooks.py` now subscribe to real flush events (caught 111 ms / 204 ms in production)
- Insights bubble state machine simplified to backend-truth-only; full WS plumbing for apply/discard with persisted confirmation
- Add static `.bg-hover` and `.border-ring-hover` utilities so the selected-state fill and the option/tick borders actually render
- Replace `border-divider/60` and `text-dim/50` (Tailwind v4 doesn't compile alpha modifiers on plain @layer utilities) with `border-ring-hover` and `text-faint` across QuestionBubble / PermissionPromptBubble / PlanBubble dimmed and locked branches
- Iterated through cyan-tinted cards, gray cards, borderless layouts, and outlined idle / filled selected before landing on the current ring-hover unification

### Compaction

- PostCompact: separate drain kick from status-flip gating; manual PostCompact drains pending queue after IDLE flip
- Compact status reads trigger from PostCompact body (drop ctx stash); `_rotate_agent_session` preserves `compact_trigger` across rotation
- Compact / clear status: stop lying via `_rotate_agent_session`

### Media / lightbox / project browser

- Lightbox: cache-bust on open + 404 fallback; two-stage error retries the original URL before declaring missing; drop unreliable onError → "missing" overlay; manual refresh button; drop cache-bust on video to prevent playback restart
- Project browser: skip cache-bust on video/audio to match lightbox; show missing-file card when viewer file is gone
- Media: retry on missing-file cards; extract shared file-existence + cache-bust primitives
- Backend `files`: extend exists-batch to accept upload paths
- SW: exclude `/api/*` from navigation fallback
- Missing-card: drop redundant retry icon — whole card is the action

### Install flow

- Cert-guide: native iOS Add-to-Home-Screen flow replaces the webclip dance; cert download opens in new tab to keep the page alive, then routes back via flag

### Scheduled / queued messages

- Inline edit for queued and scheduled bubbles with transparent auto-grow textarea; popover save/cancel/time
- Grid-mirror so view↔edit wrap identically
- Move 📅 reschedule into the main action menu
- Scheduled bubble shows relative countdown like deferred

## [0.10.7] - 2026-05-05

Patch release covering the file-attachment UX rework: missing-file detection, batch existence probing, and a single universal download path.

### File downloads

- Collapse `downloadFile` to one synchronous `<a href="…?download=1" download>` click. The backend's `FileResponse` already streams from disk in 64 KB chunks with Range support and emits `Content-Disposition: attachment`, so the browser owns progress, pause/resume, and disk writes — no JS Blob, no size threshold, no platform branching. Net −125 lines across `api.js`, `FilePreview`, `ImageLightbox`, `ProjectBrowserModal`.
- Strip `?token=…` from the URL before `parseFileUrl` matches it, so `exists-batch` resolves the actual filename instead of `<path>?token=…`.

### File existence probing

- Replace per-attachment HEAD probes with `POST /api/files/exists-batch`. FastAPI doesn't auto-register HEAD on a GET route, so every probe was returning 405 and marking attachments as missing. The batch endpoint takes `[{project, path}, …]` and returns `{exists, size, mtime}` per item in one round-trip.

### Missing-file UI

- Render a muted `[missing]` card when the resolver returns `exists: false`, instead of `[ext]` plus broken download/copy buttons. Doc-group rows probe each path independently via the shared batch result.
- Two-stage image error: fall back from `/api/thumbs/` to full-res before showing the missing-file card; videos fall through to missing if the thumbnail 404s.

### NewTask redesign

- Drop the title input — titles auto-generate via `gpt-4o-mini` from the prompt body (≤ 50 chars / ≤ 15 CJK, language-matched, anchors preserved).
- ⌘+Enter dispatches; the launch button stays mounted and greys out when no project is selected. The project-miss highlight reuses the bookmark-flash CSS class.
- Simplified icon bar: drop reminder / quick-save / inbox buttons, add a swipe-down hint, restore the inbox button next to launch.

### Misc

- Generate video thumbnails via `/api/thumbs` ffmpeg frame-grab, cached next to the source under `.thumbcache/`.
- `flash-cyan` keyframes match the bookmark breath (0.75 brightness, 0.85 saturate, 1 s × 2) and dim the selector itself rather than an outer ring.

## [0.10.6] - 2026-05-05

This release improves the in-app file viewer and preview experience: inline rendering for media/PDF, video thumbnail generation, iOS-compatible mp4 handling, and graceful fallbacks for missing files.

### Frontend

- FileViewer: render videos / images / audio / PDF inline by extension
- FilePreview: HEAD-probe doc/generic/group cards for missing paths and show a muted "missing" card when the thumb 404s
- FilePreview: move missing-file early return below `useCallback` to keep hook order stable
- TasksPage: drop gradient / shadow / `transition-all` on the AI button to avoid a GPU compositor layer
- download: guard against duplicate `share()` calls
- formatters: recognize Office formats (xlsx/xls/docx/doc/pptx/ppt) as agent attachments

### Backend

- `/api/thumbs`: generate video thumbnails via ffmpeg frame grab
- files: transcode mp4 to an iOS-compatible profile when codec / level is unsupported
- files: remux non-faststart mp4 on demand for iOS playback
- browse: remove the 512 KB file size cap

### Cleanup

- drop the attempt-1 download guard and rename the mp4 lock dict

## [0.10.5] - 2026-05-04

### Fixed

- **Voice transcription delivery hardening.** Several races could swallow a transcript when the user navigated between chats while a recording was finishing. Delivery is now atomic: `claimVoiceJob` makes IndexedDB the single ground truth (no parallel guard layers), `deleteVoiceJob` is awaited before `onTranscript` fires, transcript routing is bound to the recording's `persistKey` (so a stale tab can't claim a transcript meant for a newer one), an in-tab subscriber set delivers same-context transcripts without round-tripping the storage event, and final delivery now uses a `localStorage` draft instead of direct setState. Caller-side `recordingKey` verification added as a belt-and-suspenders guard. `feedbackVoice` pipeline + IDB job are dropped when the chat changes; `InboxCard` stops the active recording on collapse.
- **Bookmarks: deferred-delete avoids API thrash.** Per-message unbookmark now updates the local list immediately and defers the DELETE call until navigation away from the chat. `bookmarkedSet` reconciles pending deletes against the live list, and refreshes on cross-device `project_update` WS events so a bookmark removed on another device disappears here without a refetch.
- **Star unbookmark on list pages: same deferred-delete pattern.** Avoids an HTTP round-trip per tap when bulk-cleaning starred items.
- **Per-chat input state resets on agent change.** `ChatInput` and `useDraft` were retaining state across chat navigation when only the `agentId` (not the storage key) changed; both now reset/reload when their key changes, fixing stale draft bleed-through.
- **ESC key clears input via `C-u` instead of double-Esc.** Old flow (`Esc Esc + Esc`) was racy; single `C-u` is reliable.
- **Suppress Claude Code's "Resume from summary" menu on agent launch.** Menu was occasionally intercepting the first user message after a launch.
- **Startup no longer re-queues SENT-but-undelivered messages.** Previously the migrate path treated unread messages as undispatched and re-sent on next boot.
- **Startup no longer rebuilds the display file on every restart** — full rebuild was unnecessary now that incremental flush is the source of truth.

### Changed

- **Backend: `rebuild_agent` migration to `flush_agent` / targeted `_replace` appends.** `_rotate_agent_session` now uses `flush_agent`; sync compact path uses targeted `_replace` instead of a full rebuild; `PreCompact` flips agent status to `EXECUTING` so the compact window is visible in the UI. Removes the recurring full-display-file rewrites that previously fired on every compaction.
- **`/new` entry: long-press `+` opens "New Project"; short-press opens `/new/task`.** Reverts the brief `/new`-as-only-New-Project layout from earlier in this cycle. The plus button on the inbox/agents bar is now the affordance for both flows.

## [0.10.4] - 2026-05-04

Reliability fixes — three independent paths where the same content was getting re-injected into agents (or the wrong agents) without the user re-sending it. Each had been a known sharp edge that's now closed at the source rather than worked around.

### Backend — restart no longer re-dispatches sent-but-not-acked messages

Under the post-Phase-2 architecture, `status=SENT` in DB is only written *after* `send_tmux_message` returns OK, so a SENT-without-`delivered_at` row guarantees tmux already received it; `delivered_at=NULL` only means the agent's `UserPromptSubmit` hook hasn't fired (TUI modal, agent busy, agent crashed). The legacy startup migration kept moving every such row back into the pre-sent zone, where the next stop hook would re-promote it through `dispatch_pending_message` — observed in the wild as one user message dispatched 3× across 2 restarts before it was finally acked. The migration now leaves SENT rows alone; `CANCELLED` handling is unchanged.

### Backend — display files no longer rebuilt on every restart

`startup_rebuild_all` was forcing every active agent's display file through a full truncate + DB reflush + pre-sent re-append cycle on every server start. That cycle was what re-exposed the SENT-no-`delivered_at` rows above to the dispatch path (migration moves them back to queued → rebuild re-emits the queued line into the index → next stop hook picks them up). Files are append-only mirrors of DB state and stay consistent through the normal write paths, so the eager rebuild was defensive overkill. Compact and session-rotation paths still call `rebuild_agent` where it's actually needed (JSONL identity changes); the pre-sent index now loads lazily on first read.

### Frontend — voice transcript bound to recording's persistKey

`AgentChatPage` is reused across `/agents/:id` navigation (React Router doesn't remount on param change), so `persistKeyRef` and `onTranscriptRef` both flip to the new agent while the in-flight transcribe → refine pipeline keeps running. The old code delivered the final text to whichever chat happened to be visible. Snapshot `persistKey` at recording-start time, propagate it through `recorder.onstop` → save → transcribe → refine → deliver, and skip delivery if the snapshot doesn't match the current `persistKey` — leaving the "done" entry in IndexedDB for the recovery effect to pick up when the user returns to the recorded chat.

### Frontend — list-page unstar deferred until navigation

Tapping the inline star on AgentsPage / ProjectDetailPage rows used to fire `unstarSession` immediately, and the resulting `session_star_changed` WS event then patched `agent.starred=false` in the store, moving the row out of STARRED filter mid-tap (visible flicker). Lifted to a parent-page `pendingUnstars` Map flushed only on navigation away (`isActive=false` / unmount / project change); a re-tap before navigation cancels the pending entry — accidental taps are free to undo. Chat-page star toggle is unchanged (immediate). Reconcile clears stale entries when `agent.starred` flips externally.

### Backend — ESC clears input via C-u instead of Esc Esc + Esc

Stop-button-from-IDLE path swapped from `Escape Escape` + safety `Escape` to `C-u`, which clears the input line in one keystroke without the double-tap timing race that the previous combination relied on.

## [0.10.3] - 2026-05-04

Realtime sync fixes — close the WS gaps where mutations took up to 5 s to land on other tabs / devices because the page polls /api/agents (or /api/projects) every 5 s as the only convergence path. Several mutation endpoints emitted no WS event at all; star/unstar emitted one but the UI surfaces that show stars on agent rows weren't subscribed to it. Fixes here all converge on the same shape: emit on commit, subscribe on the source of truth.

### Backend — WS emits added on mutation

- `PUT /api/agents/{id}`: emit `agent_update` so deferred_to / muted / name flips propagate.
- `agent_update` payload: include deferred_to, muted, name (sparse; only when changed) so AgentsPage can patch without a follow-up GET.
- Mark-read endpoint: emit `agent_update` carrying unread_count=0.
- Project settings + task reorder: emit `project_update` / `task_update`.
- Star/unstar: targeted `session_star_changed` event (replaces the project_update path that forced AgentChatPage to refetch /sessions just to read one boolean).
- Bookmarks CRUD (POST /messages/{id}/bookmark, PATCH /bookmarks/{id}, DELETE /messages/{id}/bookmark): emit `project_update` after each commit.

### Backend — WS dispatch under sync endpoints

- `bookmarks` router endpoints are sync `def`, so they run in AnyIO's thread pool with no event loop. `asyncio.ensure_future` raised RuntimeError. Adopted the projects.py pattern: module-level _main_event_loop set during lifespan + `_emit_ws()` helper that uses `run_coroutine_threadsafe` (closes the coro if the loop isn't running yet to suppress "never awaited" warnings).

### Backend — session_star_changed payload

- Now carries `agent_id` (resolved via the same Agent.session_id == sid OR Agent.id == sid predicate as enrich_agent_briefs, covering legacy id-keyed stars). Lets AgentsPage patch the shared store keyed by agent.id without maintaining a session→agent reverse index on the frontend.

### Frontend — subscribe / patch

- AgentsPage `agent_update` handler: merge unread_count, last_message_preview, last_message_at, has_pending_suggestions, insight_status, deferred_to, muted, name when present. patchOne preserves keys the WS payload doesn't carry (sparse field semantics).
- AgentsPage: subscribe to `session_star_changed` → `patchOne(agent_id, { starred })`. Single patch updates AgentsPage rows AND ProjectDetailPage rows (both read from useAgents()).
- AgentChatPage: subscribe to `session_star_changed` for the chat-header star button (no /sessions refetch).
- AgentChatPage: subscribe to `project_update` for the chat-page sessions list. Hoisted above the agent_id guard since project-scoped events have no agent_id.
- ProjectDetailPage: subscribe to `project_update` → loadData (refreshes bookmarks + stats).

### Frontend — local-tap optimistic

- AgentChatPage star button: flip setStarred immediately, dispatch agent-star-changed for same-tab listeners, rollback on HTTP failure. Mute and rename were already optimistic; star matches now.

## [0.10.2] - 2026-05-03

### Fixed

- **Android PWA app icon now displays correctly.** Manifest icons previously pointed to local URLs (`/icon-192.png`), which Google's WebAPK minting server can't reach when the host is a private Tailscale or LAN IP — the launcher then fell back to "first character of host" (e.g. "1" for `100.x.x.x`). Manifest icons now point to a public jsDelivr CDN mirror of the GitHub release tag, which the minting server can fetch over the public internet. The version is read from `frontend/package.json` at build time so each release pins to its own immutable icon set.
- **iOS Safari "Add to Home Screen" without mobileconfig** also affected by the same private-cert fetch issue. `apple-touch-icon` link in `index.html` now uses the same jsDelivr CDN URL (with `__APP_VERSION__` injected at build time via a small `transformIndexHtml` plugin), so iOS users who skip the Web Clip flow get the bee icon instead of a generic placeholder.

### Added

- **Android-aware cert-guide page.** `/cert-guide` detects the platform from UA and shows the appropriate install steps. Android Step 1 covers the actual Android cert-install path (download `.crt`, set screen lock first, Settings → Security → Encryption & credentials → Install a certificate, with a note that path varies by ROM and Settings search works). Android Step 2 covers PWA install via Chrome menu, and uses `beforeinstallprompt` to surface a one-tap install button when Chrome fires it. A manual iOS/Android toggle at the top of the page lets users override detection.
- **`tools/bump-version.sh`** — bumps version across both `package.json` files, inserts a CHANGELOG stub, commits, and creates an annotated tag. Prints next-step reminders, including that the tag must be pushed before the build is shipped (jsDelivr won't serve icons from an unpushed tag).

## [0.10.1] - 2026-05-03

### Added

- **E-ink mode: two-finger horizontal swipe to page-scroll the chat.** Left swipe = page down, right swipe = page up. Saves the user from dragging the scrollbar on slow-refresh e-paper panels.

### Fixed

- **E-ink swipe gesture not firing.** `touchend` fires once per finger lift, and the handler was nulling `touchState` on every intermediate fire — so a 2-finger swipe only worked if both fingers happened to lift in the same frame. Now waits for `e.touches.length === 0` before processing, and preserves the original start point if a second finger lands mid-gesture.

### Changed

- **Skeleton cleanup, round 2.** `GitPage` and `ProjectDetailPage` were still flashing ghost shapes in the body during data loads (commit-row placeholders, branch pills, agent-row cards). Body region now stays empty during load, matching the prior simplification of `ChatSkeleton` / `ProjectDetailSkeleton` / `TaskDetailSkeleton` / `RouteFallback`. Header chrome (Git tab pills) preserved.

## [0.10.0] - 2026-05-02

### Added

- **Context usage pill** on the chat header. Live per-agent context-window meter with a tap-to-expand breakdown (system / tools / MCP / messages / cache split, free vs. used). Counts come straight from the Claude Code session JSONL, not estimated. Inline suggestions appear when usage gets high. Pushed over WebSocket and persisted on the agent row so the value paints immediately on chat open. Resolves [#3](https://github.com/jianpengyao/xylocopa/issues/3) point 1.
- **System / meta-agents on `.xylo-internal`.** System-level sessions (Task-AI, merge agents, insights generation, etc.) are now hosted on a synthetic `.xylo-internal` project placeholder — they no longer need to be bound to a real project to run. Templates (`CLAUDE.md`, `PROGRESS.md`, agent-hooks JSON) moved to `.xylo-internal/templates/` and loaders fail-fast if `PROJECTS_DIR` is unset. Resolves [#3](https://github.com/jianpengyao/xylocopa/issues/3) point 4.
- **E-ink display mode.** Manual toggle in **Settings → Display**, plus user-agent auto-detect for BOOX / Onyx / Kindle / Bigme / Hisense / Meebook / iReader. Flattens glass surfaces to a single page color, collapses colored badges/tags to grayscale, switches saturated bubbles to outlined style, drops gradients, and bumps secondary-text weight for readable contrast on e-paper. Includes an `?eink-diag=1` overlay for on-device detection debugging and an auto-fullscreen step on first toggle.
- **Subagent visibility.** Sub-sessions spawned via Claude Code's `Agent` tool are now discovered under `<session>/subagents/` and surfaced as a Task → Xylo session → CC session → Sub-session hierarchy in the UI; receiver-side filter on the Agents page hides synthetic subagent rows from the main list while preserving the parent linkage in chat.
- **Lifetime cost tracking** for xylo-agents. Per-agent cumulative spend, deduped by message id (so resumes don't double-count), with corrected Opus prices and 5m / 1h cache-read split.
- **Task graph visualization** on the project dashboard, with task chips on the graph tab.
- **Bookmarks (continued from 0.9.x).** Project rename data-migration chain now includes `bookmarked_messages` (FK deferred for safe rename).
- **Frame-by-frame DOM mutation logger** (`frameLogger.js`) for diagnosing UI flicker.

### Performance

- **Chat-page open is materially faster.** Parallel fetches in place of serial waterfalls; hover prefetch on agent rows so the next chat is warm by the time you tap; idle-prefetch of heavy lazy chunks (chat / project / task / new-task) so route transitions don't pay the chunk-download cost; `briefCache` so detail pages paint the real header from cached project/task data instead of showing a centered spinner.
- **Route-aware skeletons** replace top-level "Loading…" spinners on main tabs. Skeletons leave the middle blank while keeping header + composer chrome, so the chat shell paints instantly.
- **Keep-mounted main tabs.** Inbox / Projects / Agents / Git / Monitor stay mounted across navigation (visibility toggle instead of `display:none` / unmount), eliminating the loading-flash on tab switches and re-fetch-on-return.
- **ESC endpoint.** Latency cut from ~1.4s to ~570ms; double-tap Esc + safety Esc replaces `C-l`; 100ms buffer before status flips to IDLE and dispatch fires.
- **Context-usage value paints immediately on chat open** — persisted on the agent row + early `setLoading(false)` so the pill doesn't hold up the rest of the header.
- **Faster agent launch + thread-safety hardening** (carried in from 0.9.6 / 0.9.7 work and stabilized this cycle).

### Changed

- **AgentsContext / context-provider refactor.** Folders, inbox-tasks, agents, and health-status polling lifted into shared providers; AgentsPage / ProjectDetailPage / ProjectsPage read from context instead of duplicating fetches. Agents-page tasks fetch limit raised from 100 to the backend max (1000).
- **Inbox card UX overhaul.** Title becomes inline-block `contentEditable` for native cursor placement; click on empty title-row / timestamp / gap / any empty area collapses the expanded card; iOS word-snap caret placement overridden; transitions tightened to avoid jitter.
- **MCP enforcement.** `task_create` / `task_update` now require `model` and `effort` tags.
- **WebSocket realtime.** `emit_agent_update` short-circuits for synthetic subagents; `agent_update` re-emitted on insights apply / discard / generate-done so other clients flip the pill without a refetch; `new_message` emit centralized in `flush_agent`.
- **Token-counts popover** notes that values come directly from the CC session JSONL, and labels Xylo vs. CC sessions explicitly.

### Fixed

- `stop_agent` `NameError` on the retry path.
- Project rename: `bookmarked_messages` rows were not being migrated.
- Lifetime cost double-counting on resumed agents (now deduped by message id).
- Various e-ink-mode contrast and badge-rendering edge cases (saturated bg → light text/SVG, status dots inside saturated pills, popover/toast borders, inline code styling).
- `frameLogger.js`: bumped class-attr truncation 60 → 200 chars so longer Tailwind utility chains aren't cut off in mutation logs.

## [0.9.9] - 2026-05-01

Patch release covering an iOS PWA notification regression, agent-resume insight cleanup, preview-row badge layout, and a README refresh with hero image and PWA screenshots.

### Frontend — agent preview row

- Moved generating/insights badges into the preview row so they no longer push the timestamp out of place (`7d4cef7`).
- Grouped preview-row badges into a single right-aligned, vertically-centered cluster (`e0104e7`).

### Frontend — iOS PWA notifications

- Stable Service Worker `message` listener — fixes notification clicks dropped after iOS PWA resume (`4203c86`).

### Orchestrator — agent resume

- Drop pending insight suggestions when an agent is resumed (`16b3410`).
- Cancel in-flight insight generation on resume so it doesn't race with the new session (`623059b`).

### Internal

- Removed dead `delivered_at` column and `SENT→COMPLETED` migration paths from the message-delivery code (`32efefc`).

### README & docs

- Added a hero image above the nav links (`033a590`).
- Added a 6-up row of PWA screenshots (Inbox / Projects / Agents / Chat / Git / Monitor) above The Loop, reflowed to a 3×2 grid for narrow viewports (`f07cfc0`, `787051c`).
- Added a transparent 512px bee icon PNG under `docs/pwa/` (`7e9d57e`).
- Added a hook line to the Lessons Compound section (`15abe99`).

## [0.9.8] - 2026-04-30

Patch release covering UI polish across `AgentsPage` and `BookmarksSection`, a rework of glass/translucent surfaces for better cross-platform rendering, a backend pagination fix for the agents endpoint, and a scaffolder backfill fix.

### Frontend — agent row & bookmarks layout

- Replaced the Starred filter tab with an inline pin + amber star toggle on each row (`5fbf2de`).
- Reorganized `AgentRow` so time sits top-right with the star/unread badge below it (`5f171d4`, `f476fb8`, `ce54ec0`).
- Mirrored the same layout in `BookmarksSection`, aligned pencil/bookmark icon sizes, and centered the pencil vertically (`d401315`, `a904af2`, `6324cb9`).
- Dropped the `min-h-[72px]` floor on bookmark rows so density matches `AgentRow` (`a9a1de2`).
- Aligned the chat-header id pill style with the other pills (`19558af`).

### Frontend — glass-surface rework

- Made glass surfaces fully opaque, then re-introduced translucency only where `backdrop-filter` is supported (`debbc55`, `1734b53`, `25cfd30`).
- Raised composer alpha so it reads as solid even without blur (`44b546f`).
- Disabled glass on Linux desktop where `backdrop-filter` renders weakly (`d8e9a3b`).

### Backend

- Dropped the default `limit` on the agents endpoints so the UI no longer silently truncates large agent lists (`874f0fd`).

### Internal

- Scaffolder now skips the host project on backfill and folds release/commit-safety rules into the project-rules section of the generated CLAUDE.md (`3e366c7`).

## [0.9.7] - 2026-04-30

Adds an agent-callable MCP control plane: running xylocopa-managed agents can now inspect and grow orchestrator state (projects, tasks, sessions, agents) from inside their own session, via 11 new MCP tools alongside the original 6 (kept as byte-identical aliases). The surface is non-destructive by construction — a verb-axis allow/deny list rules out destructive ops at the tool name level, with no override.

### MCP control plane (new)

- **project**: `project_list`, `project_get`, `project_create`, `project_scaffold`, `project_regenerate_claude_md`. `project_create` is idempotent on name (re-activates archived projects), rolls back the DB row if registry write fails, does not clone (caller clones first).
- **task**: `task_get` (full task detail), `task_counts` (per-status counts, optionally project-scoped). Existing `task_list/task_create/task_update/task_dispatch` retained.
- **session**: `session_tail` (default 10-turn snapshot, same backend as `session_read`).
- **agent**: `agent_list` (filter by project + status), `agent_get` (full agent record by id/session_id/prefix).
- **system**: `system_health` (DB liveness, registry parseability, project/task/agent counts).

### Compatibility

- The original 6 tools (`list_sessions`, `read_session`, `create_task`, `update_task`, `dispatch_task`, `list_tasks`) remain as `@server.tool()` alias wrappers. Output strings are byte-identical to the previous release.
- Verb whitelist: `list / get / read / tail / count / health / create / update / dispatch / scaffold / regenerate`.
- Verb blacklist (will never be exposed): `delete / archive / kill / force / reset / drop / wipe / clean / cancel / stop / purge / restore / truncate / restart`. Destructive ops stay in the web UI.

### Scaffolder

- Project CLAUDE.md template now includes a "Xylocopa context" section pointing managed-project agents at the available MCP tool prefixes and the new reference doc. Existing projects pick this up on the next `project_regenerate_claude_md` call (or `backfill_all_projects`).

### Documentation

- New `docs/agent-mcp-tools.md` — canonical agent-facing reference: safety model, per-domain tool tables, what's intentionally not exposed, operational notes. Linked from README.

### Tests

- `orchestrator/test_mcp_tools.py` — standalone test (no pytest). Sets up temp `XYLOCOPA_ROOT`, exercises every tool's happy + error path, verifies alias byte-equality, tears down. 46 assertions across 23 tools.

### Frontend

- AgentsPage and ProjectDetailPage gain a Starred filter tab, ordered before Active. Deferred-section visibility is now scoped to Starred/Active tabs only (cleaner Stopped/Insights tabs). ProjectDetailPage's deferred section renders inline (revert of the brief collapsible variant).

### Insights

- All insight-generating prompts now force English output regardless of the agent's conversation language, for consistent rendering in the UI.

### Instrumentation

- Adds `ghost_probe_scan.py` — harvests `GHOST_PROBE` and `GHOST_DELIVERED` log lines into a single report for diagnosing dispatch ghosting. Probe logs added around dispatch and startup migrations.

## [0.9.6] - 2026-04-30

End-to-end CJK dispatch latency dropped from 1883ms (v0.9.5) to 1581ms (v0.9.6) across 4-sample averages. Three orthogonal changes account for the cut, plus a separate audit fixed a latent cross-thread session hazard. Sync-loop crashes (the `database is locked` failure mode that flagged 0.9.4) have not recurred since `8d2e6bf` shipped in 0.9.5.

### Backend

- `[92e9339]` Run `create_tmux_claude_session` in `asyncio.to_thread` at all four callsites (task dispatch, legacy create, launch-tmux, resume). The 5 sync `tmux` subprocess calls (kill-session, new-session, display-message, send-keys × 2) no longer stall the event loop during dispatch.
- `[a4664d5]` Stop passing the request-scoped SQLAlchemy session into `asyncio.to_thread` workers. New `_query_insights_threadsafe` / `_query_insights_ai_threadsafe` open a short-lived `SessionLocal` inside the worker so cross-thread session sharing — a latent bug masked by SQLite serialization — is gone.
- `[e3d52b4]` `TUI_SETTLE_DELAY` trimmed 0.5s → 0.3s. The buffer now covers only the React-input-handler wire-up gap; verified across 4 dispatches with no character drops.
- `[ebc9a43]` Removed the auto-dismiss of Claude Code's `/rate-limit-options` TUI menu. Users dismiss it manually with Enter; the change clears one event-loop dependency in `sync_import_new_turns`.
- `[f7b028a]` `agent_created` WebSocket event for event-driven new-agent visibility, with diagnostics added in `[690ca69]`.

### Frontend

- `[17e2561]` Optimistic `launchAgent`: sheet dismisses immediately while create + dispatch run in the background — perceived launch becomes instant.
- `[65a2bc4]` Focus-slice lazy load: clicking a bookmark now centers the message in one fetch.
- `[ed510d7]` Search results append `?focus=<msg_id>` so opening a result centers + flashes the matched message.
- `[6449721]` Yellow text highlight on matched search query.
- `[32faf50]` Search highlight survives `ChatBubble` re-renders on busy agents.
- `[4ae89ce]` `userScrolledUp` now includes `hasLater`, so focus-slice == scrolled-up state.
- `[d0c18b9]` `[e0051c3]` `[00eb25d]` `[3852a22]` `[d5469f6]` `[3aad92d]` Focus-mode + auto-pin-to-bottom corrections; `scrollToLiveTail` extracted and deferred to commit-time `useLayoutEffect`.
- `[8095d14]` `[151bec7]` Bookmark edit: stable row height, single-line textarea.
- `[68b7055]` Bookmark focus-flash: brightness pulse inverted (dim instead of brighten).
- `[6165566]` `[71e69dd]` `[82c5052]` `useLongPress`: text-selection disable baked in; no `onTap` after a swipe/scroll; iOS card long-press disables native text selection.
- `[aecad0b]` `loadNewerMessages` advances `nextOffsetRef` to the read cursor.

### Tools

- `[61affd4]` `tools/benchmark3.py` measures Chinese → English translation latency via OpenAI for diagnosing CJK dispatch slowness.

## [0.9.5] - 2026-04-29

Dispatch endpoint no longer blocks the event loop during the OpenAI translate call used by `query_insights`. CJK prompts that previously serialized translation behind TUI startup polling now run the two phases in parallel, cutting user-perceived agent launch latency from ~3.3s to ~2s. Frontend ships an expanded emoji picker, and the getting-started docs add a dedicated Bookmarks section.

### Backend

- Convert `_prepare_dispatch`, `_prepare_pre_sent_entry`, `_dispatch_task_tmux`, `_dispatch_pending_tasks`, `_tick` to async; wrap `query_insights` / `query_insights_ai` calls with `asyncio.to_thread` so the synchronous OpenAI HTTP request runs in a worker thread instead of the FastAPI event loop. Insights semantics unchanged — the translated query is still awaited and folded into the prompt.

### Frontend

- `FLUENT_MAP` expanded from 152 to 229 emoji entries (77 new entries commonly chosen by LLMs for resume hints).
- Emoji picker: 4 new tabs + keyword coverage to surface the new entries.
- Bookmarks: vertically center emoji on multi-line rows.

### Docs

- `getting-started`: dedicated section emphasizing message-level Bookmarks.

## [0.9.4] - 2026-04-29

This release fixes a long-standing issue where an expired Anthropic OAuth access token would bounce users back to the login screen 2–3 times in a row, even though their Xylocopa session was perfectly valid.

### Bug Fixes

- **Backend — `/api/system/token-usage` no longer pollutes session auth.** When Anthropic's upstream OAuth check returned 401/403 (e.g. expired access token), the orchestrator forwarded that status verbatim to the frontend. The frontend's `request()` wrapper treats any 401 as session expiry and dispatches `auth-expired` → unconditional `navigate("/login")`. Now upstream 401/403 are remapped to 502 (Bad Gateway), so they're handled as a degraded-monitor signal instead of a session signal.
- **Frontend — `auth-expired` handler still navigated even when the grace period blocked the token clear.** Reproducing the [2026-04-11] "unconditional navigation" anti-pattern: `clearAuthToken` correctly returned `false` during the 3-second post-login grace window, but `navigate("/login")` always fired. The token-usage backend fix removes the trigger; this is a separate latent bug worth tracking.

### Refactor

- **Frontend — token-usage polling fully decoupled from `MonitorContext`.** Previously bundled into the warm-up loop (1 min when monitor inactive) and active fast-poll loop (10 min when MonitorPage open). Now lives in its own `useEffect`: one fetch ~2 s after mount, then every 30 minutes, regardless of `monitorActive` or page visibility. Backend's existing 2-min cache means real cost is ~one Anthropic API call per 30 min per session.

## [0.9.3] - 2026-04-29

### Changed

- BookmarksSection rows: title editing now via an explicit pencil icon (replaces the long-press gesture); pencil and time both pinned to the top-right of the row.
- Title clamps to a single line (truncate); the original-message line below also clamps to a single line so rows stay uniform height.
- 4o-mini bookmark-title summarizer prompt tightened to 3–6 words, ≤40 chars for cleaner row display.
- Split-screen Projects tab: pane restores the last-opened project on re-entry instead of resetting to the project list.

## [0.9.2] - 2026-04-28

### Changed

- Split-screen pane chat header: replace the X (close) icon with the chevron-left back arrow and switch to `resolveBack()` logic so the pane respects the navigation state chain (e.g. A→B→back goes to A) instead of always returning to `/agents`.

## [0.9.1] - 2026-04-28

### Changed

- BookmarksSection rows: shrink primary line to 13px; swap title/description in row layout; drop the "note" pill.
- Right-side bookmark icon on a row toggles in both directions; second click on the filled icon removes the bookmark.
- Empty-state hint reworded long-press → double-tap to match the new gesture.

### Fixed

- Keep de-bookmarked rows visible until the next page mount so the row doesn't vanish mid-toggle.

## [0.9.0] - 2026-04-28

### Added

- **Bookmarked messages.** Double-tap a chat bubble → **Bookmark** to save standout turns. New `bookmarked_messages` table + `routers/bookmarks.py` CRUD endpoints. Each bookmark stores the message id, an optional user note, and a `gpt-4o-mini`-generated summary + emoji. Media references (image/file paths) are extracted from the bookmarked message and ±2 neighbors and cached. Per-project **Bookmarks** section; tapping a row scrolls to the original turn with a yellow focus-flash (2 cycles, tinted by the bubble's own color).
- Post-bookmark note prompt — compact amber pill that expands into a textarea inline; the AI summary serves as the title fallback if you skip.
- Bookmark icon on attachment action buttons (the chat-bubble menu remains the canonical entry for text bookmarks).
- Service Worker now caches Fluent UI emoji SVGs `CacheFirst`.

### Changed

- Chat-bubble interaction: **double-tap** opens the per-message action menu (Copy / Modify / Delete / Bookmark). Long-press menu trigger removed; only one menu is open at a time; outside-pointerdown auto-closes.
- MonitorContext warms its cache 2s after app mount, then background-polls every 60s while inactive.

## [0.8.9] - 2026-04-28

### Fixed

- ESC-then-queued-dispatch race that put the just-sent user bubble *above* the assistant turns and the "Request interrupted by user" bubble in chat. `dispatch_pending_message` now drains pending JSONL turns synchronously before allocating `display_seq`, so the interrupt and any in-flight agent activity get their seq first and the promoted user message lands after them in chronological order.

## [0.8.8] - 2026-04-28

### Added

- Offline-resilient adoption path for unmanaged CLI sessions: `session-start.sh` writes `/tmp/xy-pending-unlinked/pane-{key}.json` when its HTTP call fails, mirroring the existing managed-agent `xy-{id}.newsession` fallback.
- `POST /api/unlinked-sessions/replay` with a liveness check (live `claude` pane whose currently-open JSONL matches the stashed `session_id`) plus the same 4-layer guards as the live hook path. Lifespan auto-replays stashes on backend startup; the Agents page refresh button also triggers replay.

### Changed

- Removed `redispatch_stuck_queued`; folded its logic into `dispatch_pending_message`. The previous EXECUTING guard checked `agent.generating_msg_id`, which has been dead-set to `None` since the 4/27 sync_engine refactor — meaning queued messages could promote pre-sent → sent while Claude was mid-turn. The 10s age cutoff is also removed under the pre_sent architecture.

### Fixed

- `sync_full_scan` content_mismatch audit now skips USER turns; a `stop_hook_summary` landing on a USER turn previously triggered spurious drift logs.

### Removed

- "CLI sessions started while backend is offline" entry from README's Known Issues and TODO.md (now handled by the stash + replay path above).

## [0.8.7] - 2026-04-28

### Fixed

- `sync_full_scan` benign-drift branch advancing pointer past missing UUIDs. When `earliest_missing_idx >= ctx.last_turn_count`, the previous code unconditionally set `ctx.last_turn_count = len(turns)`, jumping the pointer over the missing UUID and making the next `sync_import_new_turns` slice miss it forever. Result: `tool_use` turns silently absent from DB until a later `real-drift` rewind happened to catch them. Fix: leave the pointer at `_earliest_missing_idx`.

### Added

- `DRIFT_INSTRUMENT` logging (`sync_start`, `sync_done`, `drift_detected`, `count_mismatch`, `savepoint_integrity_error`, `compact_purge`). Log-only, no behavior change.

## [0.8.6] - 2026-04-28

### Added

- Split `/display` into `/display/sent` and `/display/pre-sent` endpoints; chat state split into `sentMessages` + `preSentMessages` so delivered and queued bubbles render independently.
- `display_writer` writes `seq=0` retry marker on agent create; `dispatcher` emits `pre_sent_tombstoned` event on promote so the frontend can drop the queued bubble.
- Persist deferred-section expanded state across reloads (inbox & agents).
- Base screenshots for input bar, inbox defer, and new agent for the getting-started walkthrough.

### Changed

- Sync state machine: accumulate `_saw_*` signals across `new_turns` instead of reading only the last turn — out-of-order JSONL writes (trailing assistant after `stop_hook_summary`) no longer mask the stop signal.
- New rule: `saw_assistant_turn → EXECUTING`. Sync now flips `IDLE → EXECUTING` from JSONL truth alone, without depending on the `user_prompt` hook chain.
- ESC / interrupt: write `IDLE` directly when interrupting `EXECUTING`; send `C-l` instead of `End + C-u` for reliable input clear in the tmux pane.
- `/display/sent`: sort messages by `seq` for deterministic ordering.
- AgentChatPage compact header drops the redundant status dot, softens Stop/Resume to ghost pills, drops branch text from row 2 (icon-only worktree pill is enough).
- README notes third-party / local model support and the UI scope caveat; drops the iPad / mobile-browser known issue.

### Fixed

- Stale localStorage when an expanded inbox card is collapsed via outside-click.
- Tap-to-edit title/description on expanded inbox cards; place caret at click point on first edit tap.
- NewTaskPage: align worktree input row to the Effort selector's right edge.

## [0.8.5] - 2026-04-27

### Changed

- Persist JSONL sync pointer to DB to eliminate full history replay on restart.
- Skip stop-hook / interrupt / rate-limit side effects on initial scan.
- Skip status inference on initial / pointer-reset scan.
- Distinguish real drift from benign timing gaps in `sync_full_scan`; add EXECUTING inference + plumb compact trigger; emit missing /compact completion signals; derive last-turn signal correctly + wake_sync on tool_activity.
- Drain old JSONL + reset sync pointer on session rotation; trust DB on `_recover_agents` + stale fallback; slim `_start/_stop_generating` to in-memory only; emit `agent_update` after `rebuild_agent` on session rotation.
- Hooks only `wake_sync`, never write status directly. Router drops launch-task IDLE write + resume fallback.
- Rename `MessageStatus.QUEUED` → `SENT`; restore inbox-card tag click → popover.

### Fixed

- `UnboundLocalError` on `_time` in sync loop.

### Removed

- Dead `MessageStatus.PENDING` and `TIMEOUT` enum values.
- Client-side telemetry 20h gate; rely on Worker per-day dedup.

## [0.8.4] - 2026-04-27

### Changed

- Telemetry: drop the client-side 20h gate, `last_heartbeat` file, and `force` parameter. `record_heartbeat()` now resolves `install_id` and POSTs unconditionally; the Worker handles per-day dedup for Discord while D1 keeps the full event stream. Schedule a 24h heartbeat loop in the FastAPI lifespan so long-running orchestrators that never restart still ping daily.
- Chat-message WS events are now signal-only (no payload).
- Renamed predelivery → pre_sent across the codebase + idempotent migration for legacy rows.
- Dispatcher emits `agent_update` after `rebuild_agent` on session rotation; emits `predelivery_tombstoned` on bulk-fail.
- `sync_engine` emits the missing `/compact` completion signal and emits `new_message` for any insert (covers post-compact and CLI-typed user messages).
- Subagents are filtered from the project agent list, with a legacy backfill.
- Restored tag-click → popover on inbox cards; deferred-section header layout uses a 3-col grid.

### Fixed

- Telemetry restart-within-20h-of-previous-send no longer silently swallows the heartbeat.

### Removed

- Dead `agent_stream` token-streaming code path.
- Eight earlier `agent_update` / `task_update` emit additions (agents.stop, regenerate-insights, insights success, mark_read, tasks.dispatch, dispatcher notify_at, retry-summary, suggestions) that caused redundant/incorrect updates.

## [0.8.3] - 2026-04-26

### Added

- **Long-press multi-select unified** across every list surface — Inbox tasks, Agent rows, Project tiles, agent rows inside a project's detail page, Trash rows. Long-press pre-selects the pressed item.
- Bulk action bars per surface: Inbox (AI batch-process / Start / Delete); Agents and ProjectDetailPage agent list (mark Read / Stop / Delete); Projects (Activate / Archive / Delete with uniform-state enabledness); Trash (Restore / permanently Delete).
- One-shot startup backfill in `database.py` promoting `parent_id`-set rows with `is_subagent=0` to `1`, protecting against future drift.

### Changed

- Selected card state unified across surfaces: `ring-2 ring-cyan-500/50 brightness-[0.88]` with a 400ms `cubic-bezier(0.22,1.15,0.36,1)` transition that includes the `filter` property.
- Bulk-bar buttons now share size, layout, and disabled treatment (`flex-1 min-h-[40px]` icon-buttons, `disabled:opacity-50 disabled:cursor-not-allowed`); Agents' Delete switched from `bg-red-900` to the standard `bg-red-600`.
- Select-mode header bars use a 3-col grid (Select All / N selected / Done) for a centered middle label.
- README's Gestures & Shortcuts section documents long-press → multi-select and the per-surface bulk actions.

### Fixed

- `GET /api/projects/{name}/agents` was missing the `Agent.is_subagent == False` filter — subagents could appear in a project's agent list until they hit the SubagentStop hook.

### Removed

- Per-card check-circle indicator on Agents and Projects (the ring + darkening is the single visual language).
- Clipboard-icon toggle button from the Inbox and Agents top bars (long-press is the sole entry point).

## [0.8.2] - 2026-04-26

### Changed

- `WorktreePill` interaction model now matches the `xylo id` pill: hover (mouse) opens the popover, long-press (touch) opens it, double-click copies. Outside `pointerdown` closes; popover hover cancels the close timer.
- Both the `xylo id` and `worktree` popovers gain an upward arrow rendered after content so it paints over the seam to form a continuous shape.
- Replaced popover drop-shadow with a reusable `.shadow-popover` utility (`0 4px 20px @ 14%` below + `0 -1px 6px @ 6%` above) — softer macOS-style halo matching the existing `.glass-bar` aesthetic.

### Removed

- Insights-status tags (`failed` / `generating` / `insights`) from the chat-page top bar; the dedicated insights surface remains the source of truth.

## [0.8.1] - 2026-04-26

### Added

- New shared `WorktreePill` component: icon-only purple chip; single-click expands a centered popover showing `worktree: <name>` plus a Copy button. Used in `AgentRow` (Agents page + Project detail), `InboxCard`, and the `AgentChatPage` header — chat list / project page / agents list / inbox / chat header now share the same compact tag.

### Changed

- Trigger uses `span` + `role="button"` so it nests inside the AgentRow card button without invalid HTML.

### Removed

- Dark/light theme toggle from the chat-page icon toolbar; theme toggle remains available on Inbox, Agents, Projects, Monitor, Tasks, Git, Trash, Split, and New pages. The chat-page toolbar is now scoped to agent-specific actions (refresh / browse / mute / defer).

## [0.8.0] - 2026-04-26

### Added

- New row 2 in the chat header hosts task and agent-id as interactive pills. ID pill is labelled `id` (4 chars, sized for tap targets); hover or long-press shows a portal-rendered popover (escapes `overflow-x` clipping, centered under the pill, prefixed `xylo id:`); double-click copies.

### Changed

- Tags collapsed onto a single line; action buttons (Stop/Resume/OK) replaced with an icon toolbar in tinted style.
- Status dot moved next to the title, matching `AgentRow` card spacing and the running-pulse on `ProjectsPage`.
- Tag row aligned with the card-list visual style.

### Removed

- Noisy chips: `model`, `effort`, `tmux`, and the `deferred` chip (which now also auto-hides once the defer time has passed).
- Native `title` tooltip on the ID pill (avoid duplicate-popover flicker); the overlay that used to intercept hover/dblclick was removed.

### Fixed

- Monitor health chip restored after an earlier drop.

## [0.7.1] - 2026-04-25

### Fixed

- Telemetry `daily_heartbeat` was reporting `v0.6.1` after the v0.7.0 release because `_load_version()` read the root `package.json` (the `create-xylocopa` npm installer's own version), which the v0.7.0 release commit didn't bump. Version source switched to `frontend/package.json` — release flow already bumps that on every tag, so heartbeat version stays in sync automatically going forward.

### Changed

- Sent-bubble check icon brightened from `gray-400/50` to `gray-100/80`; sent bubble matches delivered colour while the grey check distinguishes the two states.

### Removed

- Stale refactor plan docs and references to them in source comments.

## [0.7.0] - 2026-04-24

### Added

- Anonymous `daily_heartbeat` telemetry (one event/day, gated to >20h interval) sent to a Cloudflare Worker that writes to a private D1 database. No IPs, no user content. Opt-out via the Monitor page toggle, `XYLOCOPA_TELEMETRY=0`, or `telemetry: false` in `~/.xylocopa/config.yaml`. See `## Telemetry` in README and [`orchestrator/telemetry.py`](orchestrator/telemetry.py).
- Defer agent: hide an agent from the main list and mute its notifications until a chosen time. Collapsible "Deferred" section on the Agents page. Defer chip uses an hourglass icon in both the inbox card and chat header.
- Per-message bubble menu in chat with Copy / Modify / Delete actions.

### Changed

- **Pre-delivery refactor.** Messages now flow PENDING → pre-delivered → sent → delivered. Dispatcher promotes pre-delivery → sent atomically on tmux send; display file uses read-before-truncate rebuild; sync content-matching is restricted to sent-state DB rows. WebSocket emits `predelivery_*` and `message_sent` events; frontend chat bubbles render the new state machine.
- DELETE is now single-step (tombstones queued/scheduled rows immediately) and ESC does soft-cancel through a separate endpoint — they no longer share behavior.
- SessionStart hook is the canonical launch-wake signal. JSONL polling fallback removed from the launch path; orchestrator waits on the hook.
- `/compact` and `/clear` slash-commands emit `message_executed` on completion; web slash-command rows are matched against their `<command-message>` JSONL wrapper instead of creating duplicate CLI rows.
- `datetime-local` inputs in the frontend prefill in the local timezone rather than UTC.
- README hero rewritten ("Many projects. One attention."), install simplified to a curl one-liner, GTD framing added.
- Tmux launch poll tightened to 200 ms; `TUI_SETTLE_DELAY` reduced from 3 s to 0.5 s.

### Fixed

- `cancel_message` is idempotent when the row is already `CANCELLED`.
- Tmux send no longer breaks on messages that begin with `-`.
- DB fallback no longer leaks `CANCELLED` messages back into display output.
- Duplicate pre-delivery bubble and out-of-order Attempts panel in chat.
- First message after orchestrator startup is synced immediately rather than waiting for the next tick.
- Sync engine restores task-launched rows to the match-candidate pool so they don't get orphaned.

### Removed

- Dead `/messages` endpoint and stale `emit_message_update(CANCELLED)` WS event.
- `XY_QUEUED_FALLBACK` scaffolding (Phase 3 cleanup of the predelivery migration).
- Unreachable partial-output salvage path in the dispatcher and the corresponding README claim.
- Telegram notification claims from docs (the integration was never implemented).

## [0.6.1] - 2026-04-23

### Changed

- Agent chat back button returns to the originating page (project detail or Agents list) instead of always going to a default.
- Inlined the back chevron into the project header so the title and nav share a single row.
- Pinned the project detail `FilterTabs` to the header, matching the sticky behavior of `AgentsPage`.
- Agent → orchestrator callbacks switched from the HTTP token channel to MCP tools; legacy callback token path removed.
- `session_source_dir()` now resolves `project_path` via `realpath`, fixing symlinked-project edge cases (follow-up to the `ef07c26` /clear rotation fix).
- Bumped `package.json` version to `0.6.1` (was stuck at `0.4.1` across v0.5.0 and v0.6.0).

### Removed

- In-project "New Agent" card — the New Task sheet is now the sole entry point for creating agents inside a project.
- Reverted the folders-endpoint union change while keeping `reconcile.py` (the union broke a downstream consumer; reconciliation logic is preserved).

## [0.6.0] - 2026-04-23

### Added

- **Resume hint** — per-project LLM-generated mood + recap card on each project tile. Anchored to three signals (task name, latest user intent, last 8 turns) so the recap doesn't drift into whatever tangent happened most recently. Regenerated on the stop hook.
- New Fluent UI Emoji set for project cards (`ProjectRing` + `FluentEmoji` components, MIT-licensed, jsdelivr CDN with system-emoji fallback). Inline emoji editor wired into both `ProjectsPage` and `ProjectDetailPage`.
- Day/week toggle on the time-badge popover.
- New delete action on cancelled message bubbles.
- Beginner getting-started guide (en + zh).

### Changed

- ~54× reduction in token consumption for MCP cross-session references.
- Pipeline-order status bar replaces the stat text strip — color-coded by task status.
- Compact single-row project strip; dropped the progress ring around the icon.
- "Task" toggle defaults to ON when creating new agents.
- Persist the voice transcription/refine pipeline in IndexedDB so it survives page reloads. `keepalive: true` on transcribe/refine fetches; `MediaRecorder.start(1000)` timeslice for long recordings.
- Inject a short-TTL `XYLOCOPA_AGENT_TOKEN` into spawned tmux agents; harden tmux create + use the worktree cwd on agent resume.
- `realpath` worktree paths in session JSONL resolution; triage meta-agent sessions attributed to the self-host project.
- `archive` blocked while sessions are active; tasks are unassigned rather than cancelled.

### Fixed

- Race where unmounting the recorder deleted its IndexedDB entry mid-flush.
- Pending interactive cards now dismiss on ESC / interrupt.

### Removed

- Git-remote chip from project cards and agent/task counts from the project detail header.
- Active Agents section from the Monitor page.
- Frustrated-face emojis from the resume-hint mood palette; re-added 🤯 as "mind-blown".

## [0.5.0] - 2026-04-21

### Added

- New `UnreadProvider` centralizing per-agent unread counts on the WebSocket event stream (`agent_update`), with HTTP resync on WS (re)connect. BottomNav Agents badge, FAB, and PWA app badge render from one source — no more 5s poll divergence.
- `AttentionButton` (renamed from the split-screen FAB): draggable, turns into a cyan unread total when any agent has new messages, tap jumps to oldest-unread (FIFO), long-press opens split screen.
- Viewing-time stats popover on the Projects header, aligned with the Weekly Success Rate layout. Press-and-hold a daily bar reveals its duration; a dim duration label sits above every bar so magnitudes are readable without interaction.
- Per-project session viewing time tracking (powers the new popover).

### Changed

- `/compact` handling: drain pending JSONL turns in `PreCompact` before pausing sync, and defer the single-check until after the drain completes.
- `mark_delivered` on slash-command delivery transitions `QUEUED → EXECUTING` and emits a `message_update` event so the UI can drop muted "pending" styling.
- `agent_update` WS payload now carries `unread_count` and message preview.
- Push fanout is fire-and-forget so it doesn't block the event loop.
- Restart-button reload wipes the service worker and caches before reloading.
- Installer prints a system-package-manager install hint instead of auto-invoking `sudo`.
- Monochrome cyan palette + roomier tooltip on the viewing-time popover; viewing-time ring replaced by a duration pill.

### Removed

- "All Tasks" section from the Weekly SR popover and the project-detail SR popover; percentage text stripped from popover numbers (first-attempt + daily sparkline labels keep them).

## [0.4.1] - 2026-04-20

### Added

- Orchestrator startup `WARNING` if any `frontend/src` file is newer than `frontend/dist/index.html`, making stale-build deploys visible immediately.
- Reload-storm detector that piggybacks on the reload-trace beacon and logs `ERROR` when a single client IP triggers ≥5 patch-failed reloads in a 60s window.
- New git `post-commit` hook auto-rebuilds `frontend/dist` when a commit touches `frontend/*` (excluding `dist/`, `node_modules/`, `dev-dist/`); serialized via `flock`, installed through `tools/install-git-hooks.sh`.
- Navigation and API timing console logs to aid client-side perf debugging.

### Changed

- `install.js` runs the step-1 dependency check before `git clone`, so users without `git` get the auto-installer guidance instead of a raw clone failure.
- pm2 → systemd migration: `install.js` prompts for auto-start independently of "start now"; `run.sh restart` refreshes `dump.pm2` so boot-time resurrect matches current config.
- npm package version aligned with the git tag (was stuck at 1.0.0).

### Fixed

- New-agent card overflow on the project detail page — Model/Effort pills now wrap inside the card on narrow viewports instead of pushing the right-side toggles off-screen.

## [0.4.0] - 2026-04-19

### Added

- **Jump-to-Unread FAB.** The split-screen FAB morphs into a cyan unread-count badge when any agent has new messages; tap jumps to the oldest unread (FIFO), long-press always opens split screen.
- New `GET /api/agents/unread-list` endpoint returns unread agents sorted oldest-first.
- New `tools/push_reset.py` — interactive picker that sends a remote SW-reset push to a specific device, unblocking PWAs wedged on a stale bundle.

### Changed

- Badge updates are event-driven over WebSocket (`new_message` / `agent_update`) with 150ms debounce; 5s poll kept as a reconnect safety net.
- `_send_webpush` fans subscribers out through a 16-thread pool instead of looping serially; total time `sum(rtt)` (~2.5s across 11 subscribers) → `max(rtt)` (~250ms).
- ESC button now triggers `wake_sync` so the cancelled-bubble state lands in the UI without waiting for the next poll tick. `loadData` defers 150ms after ESC wake-sync so the refresh lands after the sync pass.
- Cancelled message bubbles render with a gray background.
- Service-worker auto-reload disabled to break an iOS PWA reload loop. Cache-buster `CV` bumped v2 → v3 to force stuck PWAs to unregister the old SW.
- `./run.sh restart` and `/api/system/restart` auto-rebuild stale `frontend/dist/` before restarting. Vite dev → `vite preview` in production to avoid HMR-reconnect white screens.
- README hero slimmed to a single bold tagline; jump-to-unread FAB added to the Monitor feature list.

### Fixed

- `sync_engine` stop-hook branch emits `agent_update` immediately after the unread bump instead of waiting for the full turn import + push fanout.

## [0.3.2] - 2026-04-19

### Added

- New skill picker panel in the chat input — frequency-sorted, slash-triggered, scans both `~/.claude/skills/` and per-project `.claude/commands/` markdown.
- Decoupled skill enumeration into its own module (`skills.py`) with per-project cache and parser folding.
- Hybrid allowlist + `KNOWN_PROBLEMATIC` blocklist for slash-command gating.
- Per-agent tab title and favicon (hue derived from agent ID, skipping the blue band).
- Permission-mode segmented three-way switch (Normal / Auto / Plan), synced to `agent.skip_permissions`.
- New `xhigh` effort level for Opus 4.7 CLI (between `high` and `max`).

### Changed

- Queued messages cancelled by the user are soft-cancelled — the row stays in the DB with `CANCELLED` status, the bubble greys in place rather than disappearing.
- `Esc` bulk-cancels all active queued messages.
- `dispatch_pending_message` gained a busy guard — refresh / wake-sync no longer send-keys into an `EXECUTING` pane.
- `_stop_generating` auto-dispatches the next `PENDING` message on every `EXECUTING → IDLE` transition.
- Streaming poll tightened to 100ms; permission re-check stays at ~30s.
- `.mcp.json` uses an absolute venv python path so MCP works regardless of CWD.
- Open agent chat in a new tab from list views.
- README repositioned around AI-native GTD; surfaces crash-recovery and durability properties.

### Fixed

- `/plugin` blocked because the TUI marketplace UI never fires `UserPromptSubmit`, leaving messages wedged.
- `<command-message>` wrapper filtered from chat history (universal slash-command JSONL injection that lacks `isMeta`).
- `AuthGuard` recovers softly when the backend reconnects instead of forcing a full reload.

## [0.3.1] - 2026-04-18

### Added

- `create_task` MCP tool — any Claude Code session can drop a task into the inbox without DB scripts or auth navigation. Reuses the `TaskCreate` Pydantic schema; `project` is optional and falls back to longest-prefix `cwd` match against `projects.path`.
- README "Durable by Default" section with source-linked bullets covering session cache, unlimited retention, partial-output salvage, tmux-anchored recovery, resume, backups, draft persistence, session-dir migration, orphan cleanup. Features table renamed "Backups" → "Reliability & Recovery".
- Reload-trace probe: every reload trigger (SW `controllerchange`, explicit `location.reload()`, vite HMR full-reload, iOS background kill) is logged via `sendBeacon` to `/api/debug/auth-diag`.

### Changed

- `AuthGuard` performs soft recovery on server reconnect instead of full page reload.
- `location.reload` override installed in `<head>` (before any ES module) detects vite-originated reloads via stack trace and suppresses them — works around vite HMR client reloading on every WS reconnect.
- `/api/debug/frontend-state` gated behind a localStorage flag.

## [0.3.0] - 2026-04-18

### Changed

- **Rebrand AgentHive → Xylocopa.** Backend renamed with `agenthive` compat shims; frontend rebrand with localStorage migration (`agenthive_*` → `xylo_*`); CLI / installer / scripts renamed (`ah` → `xy`). Tmux pane prefix switched to `xy-{agent_id[:8]}`; legacy `ah-` sessions still recognized. GitHub repo moved to `jianpengyao/xylocopa`. Bee mascot (carpenter bee, *Xylocopa*) replaces robot icon; PWA icons regenerated.
- LaTeX math rendering via KaTeX in the chat view.
- Media file extraction from tool-usage entries (absolute paths supported) for inline preview.
- All images render via `FileAttachments` thumbnails; inline duplicates suppressed.
- Agent cards redesigned to match Inbox style — tag pills, single-line preview truncation. Hollow status ring with radiating glow for executing; cyan-family palette for idle/stopped. Drag-and-drop reordering with `sort_order` column migration.
- FloatingTaskCard redesigned with structured metadata layout and InboxCard-style tag pills. Note module added (markdown rendering, no border). **Quick Note** renamed from Notes — personal memo, not model input.
- Unlock screen redesigned as horizontal liquid-glass card.

### Fixed

- Stuck `pendingSendRef` causing auto-send after failed upload.
- Deferred-send decoupled from text changes via refs.
- File browser localStorage scoped per-agent-session; stale project-scoped keys cleared.

## [0.2.1] - 2026-04-14

### Added

- LaTeX math rendering — chat messages now render LaTeX formulas via KaTeX. Block math (`$$...$$`) displays as centered equations; inline math (`$...$`) renders within text. Requires at least one LaTeX marker (`\`, `{`, `^`, `_`) to avoid false positives with currency symbols.
- Tool-usage media extraction — file paths mentioned in tool calls (e.g. `Read /path/to/image.png`) are detected and displayed as media previews in the following text bubble.
- Known Issues section in the README documenting iPad and mobile-browser layout caveats.

## [0.2.0] - 2026-04-13

### Added

- macOS support: process detection (`pgrep`/`ps`) moved into a platform abstraction layer; macOS Keychain support for Claude credential reading; NFC Unicode normalization and CWD fallback for JSONL path resolution on APFS; Bash 3.2 quoting fix in `session-start.sh`. iOS CA certificate install guide, Web Clip profile generation, mkcert CA root support.
- Cross-session MCP — agents read other agents' conversation history via `"check ah session <session_id>"`.
- Retry-adjusted stats: progress metrics account for retried tasks; unified formula across TaskRing, weekly rate, and daily sparkline.
- Timezone support for daily/weekly stats.
- Task toggle on agent creation forms — optionally create a tracked task when launching an agent.
- Gestures documented: double-tap to copy session IDs and messages.

### Changed

- Viewport: `h-dvh` → `fixed inset-0` migration for iOS Safari bottom gap; `overscroll-behavior` to prevent rubber-band scroll breaking layout.
- Import retry with loading fallback for iOS PWA background resume; split-screen `h-dvh`/`h-screen` inconsistency fix.
- Unified split-screen and single-screen navigation bar layout.
- File browser converted from full-screen modal to bottom sheet with cached state.
- Code copy button always visible on mobile, hover on desktop. Scrollable code blocks and tables inside chat bubbles.
- Chat bubble width tuned to `min(85%, 30rem)`.
- Push notification taps preserve split-screen mode.
- `skipWaiting` + `clientsClaim` for instant Service Worker updates.

### Fixed

- Orphaned tmux sessions properly killed on agent stop.
- Page navigation lag (polls no longer restart on every route change).
- Rate-limit-options menu auto-dismiss after rate limit clears.

### Removed

- Broken Agents-by-Status card and redundant Claude Processes card from Monitor.

## [0.1.1] - 2026-04-13

### Changed

- **Session detection simplification.** Replaced 4-strategy `_discover_session_id_from_pane()` fallback chain with a single `SessionStart` hook path. The hook now writes `session_id` at entry creation time; the adopt endpoint reads it directly without discovery.

### Removed

- Multi-strategy discovery (file descriptor scanning, JSONL freshness-window matching, `/tmp/ahive-pending-sessions/` signal mechanism, shell script offline fallback).

### Added

- Commit-safety rules added to `CLAUDE.md` (no secrets, no certs, no personal paths, no database files).

## [0.1.0] - 2026-04-13

### Added

- Multi-agent orchestration with tmux-based sessions
- Real-time WebSocket communication for live agent output streaming
- Project management with git integration and isolated worktrees per agent
- Voice input support via OpenAI Whisper for hands-free task creation
- Mobile-responsive PWA interface with Add to Home Screen support
- Task management inbox with drag-to-reorder priorities
- Agent coordination with configurable concurrency limits and timeouts
- Session persistence and JSONL-based history with crash recovery
- Push notifications for agent status changes (finish, error, needs input)
- Password authentication with rate limiting and inactivity-based session lock
- HTTPS with self-signed certificate generation for LAN encryption
- System monitor for disk, memory, and GPU usage
- CLI session sync (read-only import and live-tail of terminal sessions)
- Dark/light theme with system-aware toggle
- Automatic hourly database backups
