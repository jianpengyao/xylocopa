# Contributing to Xylocopa

Thanks for your interest in contributing to Xylocopa! Whether you're reporting a bug, suggesting a feature, or submitting code, your help is welcome.

## Reporting Bugs

Open a [GitHub issue](https://github.com/jianpengyao/xylocopa/issues) with:

- A clear, descriptive title
- Steps to reproduce the problem
- Expected vs. actual behavior
- Relevant logs (check `logs/server.log` and `logs/orchestrator.log`)
- Your environment (OS, Python version, Node version, browser)

## Suggesting Features

Open a [GitHub issue](https://github.com/jianpengyao/xylocopa/issues) and label it as a feature request. Describe the use case and why the feature would be valuable. If you have ideas about implementation, include them, but the "why" matters more than the "how."

## Development Setup

### Prerequisites

- Linux (Ubuntu 22.04+ recommended)
- Python 3.11+
- Node.js 18+ and npm
- tmux
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)

### Getting Started

```bash
# Clone the repo
git clone https://github.com/jianpengyao/xylocopa.git
cd xylocopa

# Set up Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r orchestrator/requirements.txt

# Set up frontend
cd frontend && npm install && cd ..

# Configure environment
cp .env.example .env
# Edit .env — at minimum set HOST_PROJECTS_DIR

# Generate self-signed SSL certs (./setup.sh does this for you; manual equivalent:)
mkdir -p certs
LAN_IP=$(hostname -I | awk '{print $1}')
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout certs/selfsigned.key -out certs/selfsigned.crt \
  -subj "/CN=xylocopa" \
  -addext "subjectAltName=DNS:xylocopa,DNS:localhost,IP:127.0.0.1,IP:${LAN_IP}"

# Start the application
./run.sh start
```

## Running Tests

Backend:

```bash
cd orchestrator && python3 -m pytest tests/
```

Frontend:

```bash
cd frontend && npx vitest run
```

## Architecture

Before making backend changes, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
It's a contributor's map of the codebase, especially the **four-layer message
sync pipeline** (JSONL → DB → display file → WebUI) and the single content-sync
entry point (`AgentDispatcher.wake_sync`). A number of non-obvious invariants
live there; violating them tends to produce subtle bugs rather than loud
errors.

## Code Style

- Follow existing patterns in the codebase. Don't introduce new conventions.
- Match the indentation, naming, and structure of surrounding code.
- No unnecessary refactors or renames, keep changes focused on the task.

## State & Sync Logic

When code synchronizes state with an external source (TUI pane, JSONL stream,
filesystem, another browser tab, another agent), these rules are non-negotiable:

- **Detect ground truth, don't guess.** Read directly from the authoritative
  source (e.g., parse `tmux capture-pane` output, replay JSONL, check file
  mtime, query the DB). State derived from partial signals, stored history, or
  "what we last sent" will drift.
- **No optimistic / guessed state for sync.** If you flip UI or DB state before
  confirming the underlying source agrees, anything else touching the source
  (user keystroke in tmux, process restart, parallel tab, another contributor)
  will silently desync you with no recovery path. Always read-back to confirm,
  and reconcile when the read disagrees.
- **Full-scan is deprecated.** New sync code must be event-driven or use a
  cursor/watermark (`last_seen_seq`, `mtime > last_check`, file watcher,
  WebSocket push). Re-scanning every record on every tick does not scale and
  will be rejected in review.

## Submitting a Pull Request

1. Fork the repository and create a branch from `master`.
2. Keep PRs small and focused, one logical change per PR.
3. Write a clear description of what the PR does and why.
4. Make sure tests pass before submitting.
5. Use the commit message format: `[scope] brief description` (e.g., `[frontend] fix image zoom gesture`).

## License

By contributing to Xylocopa, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
