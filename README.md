# Turbot

A self-updating Discord bot powered by Claude. Turbot can chat, accept feature requests, generate its own code changes via PR, and auto-deploy when PRs are merged. If a deploy fails, it rolls back automatically.

## Prerequisites

- Python 3.12+
- A [Discord bot token](https://discord.com/developers/applications)
- An [Anthropic API key](https://console.anthropic.com/)
- A GitHub repo with the [GitHub CLI (`gh`)](https://cli.github.com/) authenticated
- A GitHub webhook pointing at your server (for auto-deploy)

## Installation

```bash
git clone https://github.com/flound1129/turbot.git
cd turbot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `DISCORD_TOKEN` | Discord bot token | *(required)* |
| `ANTHROPIC_API_KEY` | Claude API key | *(required)* |
| `FEATURE_REQUEST_ROLE` | Discord role allowed to submit requests | `BotAdmin` |
| `GITHUB_REPO` | GitHub repo in `owner/repo` format | *(required)* |
| `WEBHOOK_SECRET` | GitHub webhook HMAC secret | *(required)* |
| `WEBHOOK_PORT` | Port for the webhook listener | `8080` |
| `LOG_CHANNEL_ID` | Discord channel ID for admin/log messages | *(required)* |
| `CLAUDE_MODEL` | Claude model for code generation | `claude-sonnet-4-5-20250929` |
| `PLANNING_MODEL` | Claude model for planning conversations | Same as `CLAUDE_MODEL` |

## GitHub Webhook Setup

1. Go to your repo's **Settings > Webhooks > Add webhook**
2. Set the payload URL to `http://your-server:8080/webhook`
3. Set content type to `application/json`
4. Set the secret to match your `WEBHOOK_SECRET`
5. Select **Pull requests** as the trigger event

## Running

Start the bot via the supervisor (recommended):

```bash
source .venv/bin/activate
python3 supervisor.py
```

The supervisor manages the bot lifecycle, handles deploys on PR merge, and rolls back if a deploy crashes within 30 seconds.

## Usage

### Chat

@mention the bot in any channel to chat. Turbot maintains per-channel conversation history (last 20 messages).

### Feature Requests (Plugin)

```
@Turbot feature request: add a command that tells jokes
```

The bot creates a **Discord thread** and starts a collaborative planning conversation. It will ask clarifying questions, propose an implementation plan, and wait for your confirmation before generating code. Once you reply **go**, it generates a sandboxed plugin in `plugins/`, scans it for security policy violations, and opens a PR. Reply **cancel** to abort at any time. Requires the `BotAdmin` role (configurable).

### Bot Improvements (Core)

```
@Turbot bot improvement: add rate limiting to chat responses
```

Same conversational flow as feature requests, but this can modify any file in the project. The PR is flagged with "CORE CHANGE" and triggers an admin channel warning.

### Feature Request Flow

1. User submits a request in any channel
2. Bot creates a thread and evaluates the request
3. Bot asks clarifying questions (1-3 at a time)
4. User and bot refine the plan collaboratively
5. Bot proposes a plan and waits for confirmation
6. User replies **go** to generate code, or **cancel** to abort
7. Bot creates a PR and posts the link in the thread

Sessions time out after 30 minutes of inactivity. Only the original requester can interact with the thread.

## Plugin System

Plugins live in `plugins/` and extend `TurbotPlugin`:

```python
from discord.ext import commands
from plugin_api import TurbotPlugin

class MyPlugin(TurbotPlugin):
    """Description of what this plugin does."""

    @commands.command(name="hello")
    async def hello(self, ctx: commands.Context) -> None:
        """Say hello."""
        await ctx.send("Hello!")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MyPlugin(bot))
```

Plugins use `self.turbot` (a `PluginContext`) for bot interactions instead of accessing the bot directly. See `plugins/example_ping.py` for a working example.

## Testing

```bash
source .venv/bin/activate
pytest tests/
```

Tests use `unittest.mock` and require no external services or environment variables.

## Architecture

**Two-process model:**
- `supervisor.py` — entry point, manages bot lifecycle, deploy, and rollback
- `bot.py` — Discord bot + aiohttp webhook server

**Deploy flow:** PR merged → webhook received → bot writes `.deploy` signal and exits → supervisor pulls latest code and restarts the bot

**Rollback:** If the bot crashes within 30 seconds of a deploy, the supervisor reverts to the last known good commit.

**API resilience:** A circuit breaker (`api_health.py`) tracks Claude API availability. After 3 consecutive connectivity failures, it stops making calls for 30 seconds (with exponential backoff up to 5 minutes), then probes with the next request to check recovery.

## Security

- AST-based policy scanner rejects unsafe plugin code (forbidden imports, builtins, dunder access) before PR creation
- Path traversal prevention in file operations
- Webhook signature verification (HMAC-SHA256)
- Role-gated feature requests
- Per-user cooldown (2 minutes between requests)
- Circuit breaker prevents cascading failures during API outages
- All generated code goes through PR review — human approval required before merge
- Plugin storage is isolated per-plugin

## Legal

- [Terms of Service](TERMS_OF_SERVICE.md)
- [Privacy Policy](PRIVACY_POLICY.md)

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
