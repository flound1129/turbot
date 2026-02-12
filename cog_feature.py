from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

import anthropic
import discord
from discord.ext import commands

import config
import github_ops
import policy

if TYPE_CHECKING:
    from bot import log_to_admin as _log_to_admin

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
SECURITY_POLICY_PATH: str = os.path.join(PROJECT_DIR, "SECURITY_POLICY.md")

_security_policy_cache: str | None = None


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


class FeatureRequestCog(commands.Cog):
    """Handles both plugin requests and bot improvement requests."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
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

        await message.reply("On it! Generating code changes... this may take a moment.")

        label = "Feature request" if request_type == "plugin" else "Bot improvement"
        await _log(
            f"**{label}** from {message.author} "
            f"in <#{message.channel.id}>: {feature_desc}"
        )

        try:
            pr_url = await self._handle_request(feature_desc, request_type)
            await message.reply(f"Turbotastic! PR created: {pr_url}")
            await _log(f"**PR created** for {label.lower()}: {pr_url}")
        except ValueError as e:
            # Policy violations — give user a specific message
            await message.reply(str(e))
            await _log(f"**{label} rejected** (policy violation): {e}")
        except Exception as e:
            await message.reply(f"Something went wrong while creating the PR: {e}")
            await _log(
                f"**{label} failed**: {e}\n"
                f"Request was: {feature_desc}"
            )

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

        response = self.client.messages.create(
            model="claude-sonnet-4-5-20250929",
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

        raw: str = response.content[0].text
        # Strip markdown fences if Claude added them despite instructions
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result: dict[str, object] = json.loads(raw)

        changes: list[dict[str, str]] = result["changes"]
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

        branch = await github_ops.create_branch(description[:40])
        github_ops.apply_changes(changes)
        await github_ops.commit_and_push(branch, summary)
        pr_url = await github_ops.open_pr(branch, title, pr_body)

        # Return to main so the working tree is clean for the bot
        await github_ops._run(["git", "checkout", "main"])

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
