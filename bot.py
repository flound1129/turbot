import asyncio
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
from collections import OrderedDict

import anthropic
import discord
from aiohttp import web
from discord.ext import commands

from api_health import claude_health, is_transient
import command_registry
import config

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE: str = os.path.join(PROJECT_DIR, ".status")
MAX_HISTORY: int = 20
DISCORD_MSG_LIMIT: int = 2000

# ---------------------------------------------------------------------------
# Discord bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
claude = anthropic.AsyncAnthropic(
    api_key=config.ANTHROPIC_API_KEY,
    timeout=anthropic.Timeout(connect=5.0, read=30.0, write=5.0, pool=10.0),
)
channel_history: OrderedDict[int, list[dict[str, str]]] = OrderedDict()
MAX_CHANNELS: int = 200


async def log_to_admin(msg: str) -> None:
    """Send a message to the designated admin/log channel."""
    channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if channel and isinstance(channel, discord.abc.Messageable):
        try:
            await channel.send(msg)
        except Exception as e:
            print(f"Failed to send to admin channel: {e}")


def _split_reply(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    """Split text into chunks that respect word boundaries."""
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find a good split point (prefer newline, then space)
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit  # No good break point — hard cut
        else:
            split_at += 1  # Include the delimiter in the current chunk
        chunks.append(text[:split_at])
        text = text[split_at:]
    return chunks


def _extract_intent(text: str) -> tuple[str, str | None]:
    """Strip intent marker from response, return (clean_text, request_type)."""
    stripped = text.rstrip()
    if stripped.endswith("[FEATURE]"):
        return stripped[: -len("[FEATURE]")].rstrip(), "plugin"
    if stripped.endswith("[IMPROVEMENT]"):
        return stripped[: -len("[IMPROVEMENT]")].rstrip(), "core"
    return text, None


async def _start_feature_request(
    message: discord.Message, description: str, request_type: str,
) -> None:
    """Bridge: hand off detected intent to the FeatureRequestCog."""
    cog = bot.get_cog("FeatureRequestCog")
    if cog is None:
        return
    await cog.start_from_intent(message, description, request_type)


@bot.event
async def on_ready() -> None:
    print(f"Turbot is online as {bot.user} — feeling Turbotastic!")

    # Sync slash command tree with Discord
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")
        await log_to_admin(f"**Slash command sync failed**: {e}")

    # Check if the deploy script left a status message for us
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                status: dict[str, str] = json.load(f)
            os.remove(STATUS_FILE)

            event = status.get("event")
            if event == "deploy_success":
                commit = status.get("commit", "unknown")
                await log_to_admin(
                    f"**Deploy successful** — now running `{commit[:8]}`. "
                    f"Feeling Turbotastic!"
                )
            elif event == "rollback":
                bad_commit = status.get("bad_commit", "unknown")
                good_commit = status.get("good_commit", "unknown")
                await log_to_admin(
                    f"**Rolled back!** Commit `{bad_commit[:8]}` crashed within "
                    f"30s. Reverted to `{good_commit[:8]}`. "
                    f"Could use some help looking into this one."
                )
            elif event == "deploy_pull_failed":
                error = status.get("error", "unknown")
                good_commit = status.get("good_commit", "unknown")
                await log_to_admin(
                    f"**Deploy failed** during git pull/install: {error}\n"
                    f"Rolled back to `{good_commit[:8]}`. "
                    f"Might need a human to take a look."
                )
            elif event == "restart":
                await log_to_admin(
                    "**Restarted** after an unexpected crash. "
                    "Keeping an eye on things."
                )
        except Exception as e:
            print(f"Error reading status file: {e}")
    else:
        await log_to_admin("**Turbot is online!** Ready and Turbotastic.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # Let cogs and commands process first
    ctx = await bot.get_context(message)
    await bot.invoke(ctx)

    # Skip chat if a prefix command was invoked (avoid double response)
    if ctx.valid:
        return

    # Only respond to @mentions
    if not bot.user or bot.user not in message.mentions:
        return

    text = re.sub(r"<@!?\d+>", "", message.content).strip()

    # Skip explicit feature request prefixes — handled directly by the cog
    if text.lower().startswith(("feature request:", "bot improvement:")):
        return

    if not claude_health.available:
        await message.reply(
            "The Claude API is currently unreachable, so I can't chat right now. "
            "I'll keep trying to reconnect — check back in a few minutes!"
        )
        return

    # Build conversation history for this channel (after circuit check so
    # rejected messages don't create dangling user turns in the history)
    history = channel_history.setdefault(message.channel.id, [])
    channel_history.move_to_end(message.channel.id)
    history.append({"role": "user", "content": text})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    # Evict oldest channels if too many are tracked
    while len(channel_history) > MAX_CHANNELS:
        channel_history.popitem(last=False)

    was_recovering = claude_health.state == "half_open"

    try:
        async with message.channel.typing():
            response = await claude.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=1024,
                system=(
                    "You are Turbot, a friendly and helpful Discord bot. "
                    "You are Turbotastic — cheerful, concise, and occasionally "
                    "make fish puns. Keep replies under a few paragraphs.\n"
                    "If the user is asking you to add a new feature or command, "
                    "end your reply with [FEATURE].\n"
                    "If they want a change to your core behavior, "
                    "end with [IMPROVEMENT].\n"
                    "Only add a marker when the intent is clear."
                ),
                messages=list(history),
            )
            if not response.content:
                raise ValueError("Claude returned an empty response")
            raw_reply = response.content[0].text

            claude_health.record_success()
            if was_recovering:
                await log_to_admin("**Claude API recovered** — circuit breaker reset.")

            reply, intent = _extract_intent(raw_reply)

            history.append({"role": "assistant", "content": reply})
            if len(history) > MAX_HISTORY:
                history[:] = history[-MAX_HISTORY:]

            if intent is not None:
                await _start_feature_request(message, text, intent)
            else:
                # Split long replies to respect Discord's 2000-char limit
                for chunk in _split_reply(reply):
                    await message.reply(chunk)

    except Exception as e:
        if is_transient(e):
            tripped = claude_health.record_failure()
            if tripped:
                await log_to_admin(
                    f"**Circuit breaker opened** — Claude API appears unreachable: {e}"
                )
            await message.reply(
                "The Claude API is currently unreachable, so I can't chat right now. "
                "I'll keep trying to reconnect — check back in a few minutes!"
            )
        else:
            await message.reply("Blub... something went wrong. Please try again later.")
        await log_to_admin(f"**Chat error** in <#{message.channel.id}>: {e}")


# ---------------------------------------------------------------------------
# GitHub webhook server (aiohttp)
# ---------------------------------------------------------------------------

def _verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        config.WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def webhook_handler(request: web.Request) -> web.Response:
    payload = await request.read()

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(payload, signature):
        await log_to_admin("**Webhook rejected** — invalid signature.")
        return web.Response(status=401, text="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return web.Response(text="Ignored event")

    data: dict[str, object] = json.loads(payload)
    pr = data.get("pull_request")
    if data.get("action") == "closed" and isinstance(pr, dict) and pr.get("merged"):
        pr_title = pr.get("title", "unknown")
        pr_url = pr.get("html_url", "")
        await log_to_admin(
            f"**PR merged** — [{pr_title}]({pr_url})\n"
            f"Deploying now. Be right back!"
        )
        # Spawn deploy script in a separate systemd scope so it survives
        # the bot service being stopped during deploy
        subprocess.Popen(
            [
                "systemd-run", "--user", "--scope", "--",
                sys.executable, os.path.join(PROJECT_DIR, "deploy.py"),
            ],
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    return web.Response(text="OK")


def _schedule_shutdown() -> None:
    """Schedule a clean shutdown via bot.close(), which unblocks bot.start()."""
    asyncio.get_running_loop().create_task(bot.close())


async def shutdown_handler(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Shutdown-Secret", "")
    if not hmac.compare_digest(secret, config.WEBHOOK_SECRET):
        return web.Response(status=401, text="Invalid secret")

    _schedule_shutdown()
    return web.Response(text="Shutting down")


async def start_webhook_server() -> web.AppRunner:
    app = web.Application(client_max_size=1024 * 1024)  # 1 MB limit
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_post("/shutdown", shutdown_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEBHOOK_PORT)
    await site.start()
    print(f"Webhook server listening on port {config.WEBHOOK_PORT}")
    return runner


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _schedule_shutdown)

    runner = await start_webhook_server()

    # Load the feature request cog
    await bot.load_extension("cog_feature")

    # Auto-load plugins
    plugins_dir = os.path.join(PROJECT_DIR, "plugins")
    if os.path.isdir(plugins_dir):
        for filename in sorted(os.listdir(plugins_dir)):
            if filename.endswith(".py") and filename != "__init__.py":
                ext_name = f"plugins.{filename[:-3]}"
                try:
                    await bot.load_extension(ext_name)
                except Exception as e:
                    print(f"Failed to load plugin {ext_name}: {e}")

    # Populate command registry from plugin files
    command_registry.init_commands_table()
    found = command_registry.scan_plugins_directory(plugins_dir)
    command_registry.rebuild_registry(found)

    try:
        await bot.start(config.DISCORD_TOKEN)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
