# Troubleshooting

- **Conversation not updating** — use the refresh button at the top of the chat to re-sync from the CLI.
- **Agent shows IDLE after a server restart but is still working** — expected; status returns to EXECUTING on its next tool call.
- **tmux naming** — don't name your own sessions with the `xy-` or `ah-` prefix; those are managed by Xylocopa.
- **PWA stuck on the loading screen** — stale Service Worker cache. Run `.venv/bin/python tools/push_reset.py` on the host, then fully close and reopen the PWA.
- **Browser certificate warnings on other devices** — expected with the self-signed certificate until you install the CA cert; per-platform steps in [install-cert.md](install-cert.md).
- **Forgotten password or `Too many failed attempts`** — run `./run.sh reset-password` on the host (`--help` lists the options). It works while the server is running, signs out every device unless `--keep-sessions` is given, and lifts login lockouts at once. A lockout only ever applies to the failing device's IP and lasts at most 15 minutes; logins from the host itself are never locked.
- **Agent hooks rejected with `403 Hook nonce mismatch`** — set `XY_HOOK_NONCE_MODE=log` (or `off`) in `.env` and restart; mismatches are logged as `HOOK_NONCE_MISMATCH` in `logs/orchestrator.log`.
- **`403 Host not allowed` or `Cross-origin request blocked`** — the backend answers only requests addressed to an IP, `localhost`, this machine's hostname or a `*.ts.net` name, and only same-origin writes. To reach it under another name (e.g. a reverse proxy on your own domain), add the name to `XY_ALLOWED_HOSTS` in `.env`. Each rejection is logged as `REQUEST_GUARD` in `logs/orchestrator.log`.

Server logs live in `logs/server.log` and `logs/orchestrator.log` in the install directory.
