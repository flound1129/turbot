"""Constrained API surface for Turbot plugins.

Plugins extend ``TurbotPlugin`` and interact with the bot exclusively through
``PluginContext`` (available as ``self.turbot``).  Direct access to the
underlying ``commands.Bot`` is intentionally hidden.
"""

from __future__ import annotations

import json
import os
from typing import Any

import aiohttp
import discord
from discord.ext import commands

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
DATA_DIR: str = os.path.join(PROJECT_DIR, "data")


class PluginContext:
    """Safe wrapper around the bot — only exposes approved operations."""

    def __init__(self, bot: commands.Bot, plugin_name: str) -> None:
        self._bot = bot
        self._plugin_name = plugin_name
        self._store_dir = os.path.join(DATA_DIR, plugin_name)

    async def send_to_channel(self, channel_id: int, content: str) -> None:
        """Send a message to a channel (truncated to 2000 chars)."""
        channel = self._bot.get_channel(channel_id)
        if channel and isinstance(channel, discord.abc.Messageable):
            await channel.send(content[:2000])

    async def http_get(self, url: str, **kwargs: Any) -> aiohttp.ClientResponse:
        """Perform an HTTP GET request via aiohttp."""
        async with aiohttp.ClientSession() as session:
            return await session.get(url, **kwargs)

    async def http_post(self, url: str, **kwargs: Any) -> aiohttp.ClientResponse:
        """Perform an HTTP POST request via aiohttp."""
        async with aiohttp.ClientSession() as session:
            return await session.post(url, **kwargs)

    @property
    def guilds(self) -> list[discord.Guild]:
        """List of guilds the bot is in."""
        return self._bot.guilds

    def get_channel(self, channel_id: int) -> discord.abc.GuildChannel | None:
        """Read-only channel lookup."""
        return self._bot.get_channel(channel_id)

    def store_get(self, key: str) -> Any:
        """Read a value from the plugin's isolated JSON store."""
        path = os.path.join(self._store_dir, f"{key}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def store_set(self, key: str, value: Any) -> None:
        """Write a JSON-serializable value to the plugin's isolated store."""
        os.makedirs(self._store_dir, exist_ok=True)
        path = os.path.join(self._store_dir, f"{key}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)


class TurbotPlugin(commands.Cog):
    """Base class for all Turbot plugins.

    Subclasses get ``self.turbot`` — a :class:`PluginContext` — as the only
    sanctioned interface to the bot.  The raw ``commands.Bot`` reference is
    stored as ``_bot_ref`` for discord.py internal machinery only.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self._bot_ref = bot
        plugin_name = self.__class__.__module__.rsplit(".", 1)[-1]
        self.turbot = PluginContext(bot, plugin_name)

    async def cog_load(self) -> None:
        """Called by discord.py when the cog is loaded."""
        if hasattr(self, "plugin_load"):
            await self.plugin_load()

    async def cog_unload(self) -> None:
        """Called by discord.py when the cog is unloaded."""
        if hasattr(self, "plugin_unload"):
            await self.plugin_unload()


# Re-export conveniences so plugins don't need extra imports
command = commands.command
listener = commands.Cog.listener
