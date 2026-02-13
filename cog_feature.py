from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anthropic
import discord
from discord.ext import commands

from api_health import claude_health, is_transient
import config
import github_ops
import policy

if TYPE_CHECKING:
    from bot import log_to_admin as _log_to_admin

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
SECURITY_POLICY_PATH: str = os.path.join(PROJECT_DIR, "SECURITY_POLICY.md")

_security_policy_cache: str | None = None

_git_lock: asyncio.Lock = asyncio.Lock()
REQUEST_COOLDOWN: float = 120.0  # seconds between requests per user
_last_request: dict[int, float] = {}

SESSION_TIMEOUT: float = 1800.0  # 30 minutes

CONFIRM_PATTERNS: set[str] = {
    "go", "yes", "proceed", "do it", "looks good", "lgtm", "ship it",
}
CANCEL_PATTERNS: set[str] = {
    "cancel", "stop", "nevermind", "nvm", "abort",
}

PLAN_READY_MARKER: str = "---PLAN_READY---"


@dataclass
class ThreadSession:
    thread_id: int
    user_id: int
    request_type: str          # "plugin" or "core"
    original_description: str
    messages: list[dict[str, str]] = field(default_factory=list)
    state: str = "discussing"  # "discussing", "plan_ready", "generating", "done"
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    refined_description: str | None = None


_sessions: dict[int, ThreadSession] = {}


def _load_security_policy() -> str:
    """Read SECURITY_POLICY.md once and cache it."""
    global _security_policy_cache
    if _security_policy_cache is None:
        with open(SECURITY_POLICY_PATH, encoding="utf-8") as f:
            _security_policy_cache = f.read()
    return _security_policy_cache


SYSTEM_PROMPT_BASE: str = """\
You are a senior Python developer working on a Discord bot called Turbot.
Given the current codebase and a feature request, produce ONLY a JSON response (no markdown
fences, no commentary) with a list of file changes:

{{
  "changes": [
    {{"path": "relative/file.py", "action": "create|modify|delete", "content": "full file content"}}
  ],
  "summary": "one-line summary of what was changed",
  "title": "short PR title"
}}

Rules:
- For "modify" or "create", always include the COMPLETE file content, not a diff.
- For "delete", content can be an empty string.
- Only change what is necessary for the feature request.
- Keep the existing code style and patterns.
- Make sure the bot still works after your changes.

"""

PLUGIN_SYSTEM_PROMPT: str = SYSTEM_PROMPT_BASE + """\
PLUGIN RULES (STRICTLY ENFORCED):
- ALL files you create or modify MUST be under the plugins/ directory.
- Plugin classes MUST extend TurbotPlugin from plugin_api.
- Use self.turbot (a PluginContext) for bot API — NOT self.bot directly.
- Use self.turbot.store_get(key) / store_set(key, value) for persistence.
- NEVER import or use: os, subprocess, sys, shutil, importlib, ctypes, pathlib, signal, socket.
- NEVER call: exec(), eval(), compile(), open(), __import__(), breakpoint().
- NEVER access: __subclasses__, __globals__, __builtins__, __code__, __class__.
- Each plugin file MUST have an async def setup(bot) function at module level.

{security_policy}
"""

CORE_SYSTEM_PROMPT: str = SYSTEM_PROMPT_BASE + """\
CORE CHANGE RULES:
- You may modify any file in the project.
- Changes outside plugins/ will be flagged as CORE CHANGES in the PR.
- Core changes require human review before merge.
- Follow all best practices in the security policy below.

{security_policy}
"""

PLANNING_SYSTEM_PROMPT: str = """\
You are a senior developer evaluating a feature request for a Discord bot called Turbot.

Your job is to have a short conversation with the user to understand what they want, \
then propose a clear implementation plan.

Guidelines:
- Assess feasibility and clarity of the request
- Ask clarifying questions (1-3 at a time, not a wall of questions)
- Keep responses short and conversational — this is Discord, not a design doc
- When you have enough information, propose a concise plan summarizing what will be built
- End your message with the exact marker on its own line when the plan is ready:
---PLAN_READY---

Do NOT include the marker until you are confident in the plan. Ask questions first if needed.
"""


def _read_project_files() -> dict[str, str]:
    """Read all .py files from the project root."""
    files: dict[str, str] = {}
    for name in os.listdir(PROJECT_DIR):
        if name.endswith(".py"):
            path = os.path.join(PROJECT_DIR, name)
            with open(path, encoding="utf-8") as f:
                files[name] = f.read()
    return files


def _read_plugin_context() -> dict[str, str]:
    """Read only plugin_api.py and existing plugins — NOT core files."""
    files: dict[str, str] = {}

    # Include plugin_api.py so Claude knows the API
    api_path = os.path.join(PROJECT_DIR, "plugin_api.py")
    if os.path.exists(api_path):
        with open(api_path, encoding="utf-8") as f:
            files["plugin_api.py"] = f.read()

    # Include existing plugins
    plugins_dir = os.path.join(PROJECT_DIR, "plugins")
    if os.path.isdir(plugins_dir):
        for name in sorted(os.listdir(plugins_dir)):
            if name.endswith(".py") and name != "__init__.py":
                path = os.path.join(plugins_dir, name)
                with open(path, encoding="utf-8") as f:
                    files[f"plugins/{name}"] = f.read()

    return files


async def _log(msg: str) -> None:
    """Lazy import to avoid circular dependency with bot module."""
    from bot import log_to_admin

    await log_to_admin(msg)


def _detect_request_type(text: str) -> str | None:
    """Detect whether a message is a plugin or core request."""
    lower = text.lower()
    if lower.startswith("feature request:"):
        return "plugin"
    if lower.startswith("bot improvement:"):
        return "core"
    return None


def _extract_description(text: str, request_type: str) -> str:
    """Extract the description after the request prefix."""
    if request_type == "plugin":
        return text[len("feature request:"):].strip()
    return text[len("bot improvement:"):].strip()


def _format_violations(results: list[policy.ScanResult]) -> str:
    """Format policy violations into a human-readable report."""
    lines: list[str] = ["**Security policy violations detected — PR not created.**\n"]
    for result in results:
        lines.append(f"**{result.path}:**")
        for v in result.violations:
            lines.append(f"  Line {v.line}: [{v.rule}] {v.detail}")
    return "\n".join(lines)


def _check_session_timeout(session: ThreadSession) -> bool:
    """Return True if the session has timed out."""
    return (time.monotonic() - session.last_active) >= SESSION_TIMEOUT


def _is_confirmation(text: str) -> bool:
    """Check if text matches a confirmation pattern."""
    return text.lower().strip() in CONFIRM_PATTERNS


def _is_cancellation(text: str) -> bool:
    """Check if text matches a cancellation pattern."""
    return text.lower().strip() in CANCEL_PATTERNS


class FeatureRequestCog(commands.Cog):
    """Handles both plugin requests and bot improvement requests."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = anthropic.AsyncAnthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=anthropic.Timeout(connect=5.0, read=90.0, write=5.0, pool=10.0),
        )

    async def _call_planning_claude(
        self, session: ThreadSession,
    ) -> str:
        """Call Claude with the planning conversation and return the response text."""
        response = await self.client.messages.create(
            model=config.PLANNING_MODEL,
            max_tokens=1024,
            system=PLANNING_SYSTEM_PROMPT,
            messages=session.messages,
        )
        claude_health.record_success()
        return response.content[0].text

    async def _handle_thread_message(
        self, message: discord.Message, session: ThreadSession,
    ) -> None:
        """Handle a message in a tracked feature request thread."""
        # Check timeout
        if _check_session_timeout(session):
            await message.channel.send(
                "This request has timed out. Start a new one in the main channel."
            )
            _sessions.pop(session.thread_id, None)
            return

        # Ignore if not the original requester
        if message.author.id != session.user_id:
            return

        # Ignore if generating or done
        if session.state in ("generating", "done"):
            return

        session.last_active = time.monotonic()
        user_text = message.content.strip()

        if session.state == "plan_ready":
            if _is_confirmation(user_text):
                session.state = "generating"
                description = session.refined_description or session.original_description
                label = (
                    "Feature request" if session.request_type == "plugin"
                    else "Bot improvement"
                )
                await message.channel.send(
                    "Generating code changes... this may take a moment."
                )
                try:
                    pr_url = await self._handle_request(
                        description, session.request_type,
                    )
                    session.state = "done"
                    await message.channel.send(
                        f"Turbotastic! PR created: {pr_url}"
                    )
                    await _log(f"**PR created** for {label.lower()}: {pr_url}")
                except ValueError as e:
                    session.state = "done"
                    await message.channel.send(str(e))
                    await _log(f"**{label} rejected** (policy violation): {e}")
                except Exception as e:
                    session.state = "done"
                    if is_transient(e):
                        tripped = claude_health.record_failure()
                        if tripped:
                            await _log(
                                f"**Circuit breaker opened** — Claude API appears "
                                f"unreachable: {e}"
                            )
                        await message.channel.send(
                            "The Claude API is currently unavailable. "
                            "Please try again in a few minutes."
                        )
                    else:
                        await message.channel.send(
                            "Something went wrong while creating the PR. "
                            "Please try again later."
                        )
                finally:
                    _sessions.pop(session.thread_id, None)
                return

            if _is_cancellation(user_text):
                session.state = "done"
                _sessions.pop(session.thread_id, None)
                await message.channel.send("Request cancelled.")
                return

            # Not confirm/cancel — treat as continued discussion
            session.state = "discussing"

        # State is "discussing" — send to Claude
        if not claude_health.available:
            await message.channel.send(
                "The Claude API is currently unavailable. "
                "Please try again in a few minutes."
            )
            return

        session.messages.append({"role": "user", "content": user_text})

        try:
            reply_text = await self._call_planning_claude(session)
        except Exception as e:
            if is_transient(e):
                claude_health.record_failure()
                await message.channel.send(
                    "The Claude API is currently unavailable. "
                    "Please try again in a few minutes."
                )
            else:
                await message.channel.send(
                    "Something went wrong. Please try again."
                )
            # Remove the user message we just added since Claude didn't respond
            session.messages.pop()
            return

        # Check for plan ready marker
        if PLAN_READY_MARKER in reply_text:
            session.state = "plan_ready"
            # Extract the plan text (everything before the marker) as refined description
            plan_text = reply_text.split(PLAN_READY_MARKER)[0].strip()
            session.refined_description = plan_text
            # Strip the marker before showing to user
            display_text = reply_text.replace(PLAN_READY_MARKER, "").strip()
            display_text += (
                "\n\nReady to generate code! "
                "Reply **go** to create the PR, or keep chatting to refine the plan."
            )
        else:
            display_text = reply_text

        session.messages.append({"role": "assistant", "content": reply_text})
        await message.channel.send(display_text)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # Check if this is a message in a tracked thread
        if isinstance(message.channel, discord.Thread):
            session = _sessions.get(message.channel.id)
            if session is not None:
                await self._handle_thread_message(message, session)
                return

        if not self.bot.user or self.bot.user not in message.mentions:
            return

        text = message.content
        # Strip the mention itself
        text = re.sub(r"<@!?\d+>", "", text).strip()

        request_type = _detect_request_type(text)
        if request_type is None:
            return  # Not a feature request — let the chat handler deal with it

        # Role check
        required_role = config.FEATURE_REQUEST_ROLE
        if isinstance(message.author, discord.Member):
            has_role = any(r.name == required_role for r in message.author.roles)
        else:
            has_role = False

        if not has_role:
            await message.reply(
                f"Sorry, you need the **{required_role}** role to submit feature requests."
            )
            return

        feature_desc = _extract_description(text, request_type)
        if not feature_desc:
            await message.reply("Please describe the feature after the request prefix.")
            return

        # Per-user cooldown
        now = time.monotonic()
        last = _last_request.get(message.author.id, 0.0)
        if now - last < REQUEST_COOLDOWN:
            remaining = int(REQUEST_COOLDOWN - (now - last))
            await message.reply(
                f"Please wait {remaining} seconds before submitting another request."
            )
            return

        label = "Feature request" if request_type == "plugin" else "Bot improvement"

        if not claude_health.available:
            await message.reply(
                "The Claude API is currently unavailable, so I can't process "
                "feature requests right now. Please try again in a few minutes."
            )
            return

        # Set cooldown after validation passes — don't burn it on circuit-open rejections
        _last_request[message.author.id] = now

        await _log(
            f"**{label}** from {message.author} "
            f"in <#{message.channel.id}>: {feature_desc}"
        )

        # Create a thread for the conversation
        thread = await message.create_thread(
            name=f"Feature: {feature_desc[:80]}",
        )

        session = ThreadSession(
            thread_id=thread.id,
            user_id=message.author.id,
            request_type=request_type,
            original_description=feature_desc,
        )
        _sessions[thread.id] = session

        # First planning call
        session.messages.append({
            "role": "user",
            "content": f"{label}: {feature_desc}",
        })

        try:
            reply_text = await self._call_planning_claude(session)
        except Exception as e:
            if is_transient(e):
                tripped = claude_health.record_failure()
                if tripped:
                    await _log(
                        f"**Circuit breaker opened** — Claude API appears "
                        f"unreachable: {e}"
                    )
                await thread.send(
                    "The Claude API is currently unavailable. "
                    "Please try again in a few minutes."
                )
            else:
                await thread.send(
                    "Something went wrong while evaluating the request. "
                    "Please try again later."
                )
            _sessions.pop(thread.id, None)
            return

        # Check for plan ready marker
        if PLAN_READY_MARKER in reply_text:
            session.state = "plan_ready"
            plan_text = reply_text.split(PLAN_READY_MARKER)[0].strip()
            session.refined_description = plan_text
            display_text = reply_text.replace(PLAN_READY_MARKER, "").strip()
            display_text += (
                "\n\nReady to generate code! "
                "Reply **go** to create the PR, or keep chatting to refine the plan."
            )
        else:
            display_text = reply_text

        session.messages.append({"role": "assistant", "content": reply_text})
        await thread.send(display_text)

    async def _handle_request(self, description: str, request_type: str) -> str:
        """Generate code changes and create a PR."""
        security_policy = _load_security_policy()

        if request_type == "plugin":
            codebase = _read_plugin_context()
            system_prompt = PLUGIN_SYSTEM_PROMPT.format(security_policy=security_policy)
        else:
            codebase = _read_project_files()
            system_prompt = CORE_SYSTEM_PROMPT.format(security_policy=security_policy)

        codebase_text = ""
        for path, content in sorted(codebase.items()):
            codebase_text += f"\n--- {path} ---\n{content}\n"

        response = await self.client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Current codebase:\n{codebase_text}\n\n"
                    f"Feature request: {description}"
                ),
            }],
        )
        claude_health.record_success()

        raw: str = response.content[0].text
        # Strip markdown fences if Claude added them despite instructions
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            result: dict[str, object] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Claude returned invalid JSON: {exc}") from exc

        if not isinstance(result, dict) or "changes" not in result:
            raise ValueError("Claude returned an unexpected response format.")

        changes: list[dict[str, str]] = result["changes"]
        if not isinstance(changes, list):
            raise ValueError("Claude returned an unexpected response format.")
        summary: str = result.get("summary", description)
        title: str = result.get("title", f"Feature: {description[:50]}")

        # AST scan gate — reject policy violations before creating PR
        if request_type == "plugin":
            violations = policy.scan_changes(changes)
            if violations:
                raise ValueError(_format_violations(violations))

        # Detect core changes
        is_core_change = any(
            not change.get("path", "").startswith("plugins/")
            for change in changes
        )

        # Build PR body
        pr_body = summary
        if is_core_change:
            pr_body = (
                "## \u26a0 CORE CHANGE\n\n"
                "This PR modifies files outside `plugins/`. "
                "Requires admin review before merge.\n\n"
                + summary
            )

        async with _git_lock:
            branch = await github_ops.create_branch(description[:40])
            try:
                github_ops.apply_changes(changes)
                changed_paths = [change["path"] for change in changes]
                await github_ops.commit_and_push(branch, summary, paths=changed_paths)
                pr_url = await github_ops.open_pr(branch, title, pr_body)
            finally:
                # Always return to main, even on failure
                try:
                    await github_ops._run(["git", "checkout", "-f", "main"])
                except Exception:
                    pass

        # Warn admin about core changes
        if is_core_change:
            await _log(
                f"\u26a0 **CORE CHANGE** in PR: {pr_url}\n"
                f"Files outside `plugins/` were modified. Please review carefully."
            )

        return pr_url


async def setup(bot: commands.Bot) -> None:
    """Entry point for discord.py extension loading."""
    await bot.add_cog(FeatureRequestCog(bot))
