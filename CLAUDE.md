# Turbot — Self-Updating Discord Bot

## What Is This?

Turbot is a Discord bot powered by Claude that can chat, accept feature requests, generate its own code changes via PR, and auto-deploy when PRs are merged. If a deploy fails, it rolls back automatically.

The name is a fish pun (turbot = flatfish + bot). The adjective is **Turbotastic** — use it generously.

## Architecture

**Two-process model:**
- `supervisor.py` is the entry point — runs `bot.py` as a subprocess, monitors health, handles deploy + rollback
- `bot.py` is the Discord bot + an aiohttp webhook server running in parallel

**Communication between them:**
- `.deploy` signal file: bot writes it when a merged PR webhook arrives, then exits. Supervisor sees it and does git pull + restart.
- `.status` JSON file: supervisor writes it before restarting the bot to communicate what happened (deploy success, rollback, restart). Bot reads it in `on_ready` and reports to the admin channel.

## Plugin System

**Two request types:**
- `feature request: <description>` — creates a **plugin** (sandboxed in `plugins/`). Claude only sees `plugin_api.py` and existing plugins. AST-scanned for policy violations before PR creation.
- `bot improvement: <description>` — modifies **core files** (elevated scrutiny). Claude sees the full codebase. PR is flagged with "CORE CHANGE".

**Plugin loading:**
- `bot.py` auto-loads all `.py` files in `plugins/` (except `__init__.py`) on startup
- Bad plugins log errors but don't crash the bot
- Plugins extend `TurbotPlugin` and use `self.turbot` (a `PluginContext`) for bot API

## File Layout

```
supervisor.py       — Entry point. Bot lifecycle, deploy, rollback
bot.py              — Discord bot + GitHub webhook server (aiohttp)
cog_feature.py      — Feature request cog (dual-path: plugin vs core)
github_ops.py       — Git/GitHub helpers (branch, commit, push, PR via gh CLI)
config.py           — Loads .env configuration
plugin_api.py       — PluginContext + TurbotPlugin base class for plugins
policy.py           — AST-based security scanner for plugin code
api_health.py       — Circuit breaker for Claude API availability
SECURITY_POLICY.md  — Machine-readable policy (injected into Claude prompts)
plugins/            — Plugin directory (auto-loaded on startup)
  __init__.py       — Package marker
  example_ping.py   — Reference plugin demonstrating correct pattern
data/               — Plugin storage (created automatically, per-plugin isolation)
```

## Key Behaviors

- **Chat**: @mention the bot → Claude responds with per-channel conversation memory (last 20 messages)
- **Feature requests**: @mention with "feature request: <description>" → role check → creates Discord thread → multi-turn planning conversation with Claude → user confirms → code gen → AST scan → opens PR
- **Bot improvements**: @mention with "bot improvement: <description>" → role check → creates Discord thread → planning conversation → user confirms → code gen → PR flagged as CORE CHANGE
- **Deploy**: GitHub webhook on PR merge → bot writes `.deploy` + exits → supervisor pulls + restarts
- **Rollback**: If bot crashes within 30s of deploy, supervisor reverts to last known good commit
- **Admin channel**: `LOG_CHANNEL_ID` — bot posts deploy status, errors, feature request activity, rollback alerts

## Conversational Feature Request Flow

Feature requests use a multi-turn thread conversation instead of one-shot code generation:

1. User @mentions the bot with a feature request in a regular channel
2. Bot creates a Discord thread from the message
3. Bot calls Claude (using `PLANNING_SYSTEM_PROMPT`) to evaluate the request and ask clarifying questions
4. User and Claude go back and forth in the thread until the plan is clear
5. When Claude is satisfied, it includes a `---PLAN_READY---` marker in its response
6. Bot strips the marker and prompts the user to confirm with "go" (or keep refining)
7. On confirmation, bot runs the existing code generation pipeline (`_handle_request`) and posts the PR link in the thread

**Thread session states:** `discussing` → `plan_ready` → `generating` → `done`

**Key rules:**
- Only the original requester's messages are processed in the thread
- Sessions time out after 30 minutes of inactivity
- User can cancel anytime with "cancel", "nvm", "abort", etc.
- Confirmation words: "go", "yes", "proceed", "lgtm", "ship it", etc.
- In `plan_ready` state, unrecognized text returns to `discussing` for continued refinement
- Thread sessions are stored in-memory (`_sessions` dict keyed by thread ID)

## Code Style

- Python 3.12+ with modern type hints (PEP 604 `X | Y` unions, no `Optional`)
- All functions have return type annotations
- All `open()` calls use explicit `encoding="utf-8"` (PEP 597)
- Module-level constants have type annotations
- Use `asyncio.get_running_loop()` not the deprecated `get_event_loop()`
- No unnecessary imports — `from __future__ import annotations` only where needed for forward refs
- Keep it simple — no over-abstraction

## Environment Variables (see .env.example)

- `DISCORD_TOKEN` — bot token
- `ANTHROPIC_API_KEY` — Claude API key
- `FEATURE_REQUEST_ROLE` — Discord role name for feature request permission (default: "BotAdmin")
- `GITHUB_REPO` — owner/repo string
- `WEBHOOK_SECRET` — GitHub webhook HMAC secret
- `WEBHOOK_PORT` — webhook listener port (default: 8080)
- `LOG_CHANNEL_ID` — Discord channel ID for admin/log messages
- `PLANNING_MODEL` — Claude model for planning conversations (default: same as `CLAUDE_MODEL`)

## CI/CD

- GitHub Actions runs on every push to `main` and PR to `main`
- Matrix: Python 3.12, 3.13
- Dev dependencies in `requirements-dev.txt` (includes pytest + pytest-asyncio)

## Testing

- Tests live in `tests/`
- Run with: `source .venv/bin/activate && pytest tests/`
- Use `unittest.mock` to mock Discord, Claude API, git subprocess calls
- Tests must not require any external services or env vars
- **IMPORTANT: Always run the full test suite and verify all tests pass BEFORE committing and pushing, never after.**

## API Resilience

**Circuit breaker** (`api_health.py`): Tracks Claude API availability with three states — `closed` (healthy), `open` (down, fast-reject), `half_open` (probing recovery). Module-level singleton `claude_health` is shared by `bot.py` and `cog_feature.py`.

**State transitions:**
- `closed` → `open`: After 3 consecutive connectivity failures
- `open` → `half_open`: After backoff expires (starts 30s, doubles to max 5min)
- `half_open` → `closed`: Probe call succeeds
- `half_open` → `open`: Probe call fails (backoff doubles)

**Error classification** — only connectivity errors trip the breaker:
- Trips breaker: `APIConnectionError`, `APITimeoutError`, `InternalServerError`, `RateLimitError`
- Does NOT trip: `AuthenticationError`, `BadRequestError` (API is reachable, problem is on our side)

**Timeouts:**
- Chat API calls (`bot.py`): 30s read, 5s connect
- Code gen (`cog_feature.py`): 90s read, 5s connect
- Plugin HTTP calls (`plugin_api.py`): 10s total (plugins can override)
- Git subprocesses (`github_ops.py`): 60s (kills process on timeout)

**Fallback behavior:** When the circuit is open, chat and feature requests get friendly rejection messages. No message queuing — clear rejection is simpler and more honest. Admin channel is notified when the circuit opens or recovers.

## Security

- **AST policy scanner** (`policy.py`) rejects plugin code that uses forbidden imports, builtins, or dunder access before any PR is created
- **Security policy** (`SECURITY_POLICY.md`) is injected into every Claude code-generation prompt — defines allowed/forbidden lists
- **Path traversal prevention** in `github_ops.apply_changes()` — rejects paths that escape the project directory
- Webhook signature verification (HMAC-SHA256) prevents spoofed deploys
- Role gating prevents unauthorized feature requests
- Claude-generated code goes through PR review — human must approve before merge
- Bot never auto-applies code, only proposes via PR
- Plugin storage is isolated per-plugin in `data/<plugin_name>/`
