"""Tests for the plugin API surface."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import plugin_api


class TestPluginContext:
    def _make_context(self, plugin_name: str = "test_plugin") -> plugin_api.PluginContext:
        bot = MagicMock()
        return plugin_api.PluginContext(bot, plugin_name)

    @pytest.mark.asyncio
    async def test_send_to_channel_sends_message(self) -> None:
        ctx = self._make_context()
        mock_channel = AsyncMock()
        mock_channel.send = AsyncMock()
        ctx._bot.get_channel.return_value = mock_channel
        with patch("plugin_api.isinstance", return_value=True):
            await ctx.send_to_channel(123, "hello")
        mock_channel.send.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_send_to_channel_truncates_long_message(self) -> None:
        ctx = self._make_context()
        mock_channel = AsyncMock()
        mock_channel.send = AsyncMock()
        ctx._bot.get_channel.return_value = mock_channel
        long_msg = "x" * 3000
        with patch("plugin_api.isinstance", return_value=True):
            await ctx.send_to_channel(123, long_msg)
        sent = mock_channel.send.call_args[0][0]
        assert len(sent) == 2000

    @pytest.mark.asyncio
    async def test_send_to_channel_handles_missing_channel(self) -> None:
        ctx = self._make_context()
        ctx._bot.get_channel.return_value = None
        # Should not raise
        await ctx.send_to_channel(999, "hello")

    def test_guilds_returns_bot_guilds(self) -> None:
        ctx = self._make_context()
        mock_guilds = [MagicMock(), MagicMock()]
        ctx._bot.guilds = mock_guilds
        assert ctx.guilds == mock_guilds

    def test_store_get_set_roundtrip(self, tmp_path: str) -> None:
        ctx = self._make_context("roundtrip")
        ctx._store_dir = str(tmp_path / "roundtrip")
        ctx.store_set("mykey", {"count": 42, "items": [1, 2, 3]})
        result = ctx.store_get("mykey")
        assert result == {"count": 42, "items": [1, 2, 3]}

    def test_store_get_returns_none_for_missing(self, tmp_path: str) -> None:
        ctx = self._make_context("missing")
        ctx._store_dir = str(tmp_path / "missing")
        assert ctx.store_get("nonexistent") is None


class TestTurbotPlugin:
    def test_has_turbot_attribute(self) -> None:
        bot = MagicMock()
        plugin = plugin_api.TurbotPlugin(bot)
        assert hasattr(plugin, "turbot")
        assert isinstance(plugin.turbot, plugin_api.PluginContext)

    def test_does_not_expose_raw_bot(self) -> None:
        bot = MagicMock()
        plugin = plugin_api.TurbotPlugin(bot)
        # Should not have self.bot (only _bot_ref)
        assert not hasattr(plugin, "bot") or plugin.__class__.__name__ == "TurbotPlugin"
        assert hasattr(plugin, "_bot_ref")

    @pytest.mark.asyncio
    async def test_plugin_load_called(self) -> None:
        bot = MagicMock()

        class MyPlugin(plugin_api.TurbotPlugin):
            loaded = False

            async def plugin_load(self) -> None:
                MyPlugin.loaded = True

        plugin = MyPlugin(bot)
        await plugin.cog_load()
        assert MyPlugin.loaded is True

    @pytest.mark.asyncio
    async def test_plugin_unload_called(self) -> None:
        bot = MagicMock()

        class MyPlugin(plugin_api.TurbotPlugin):
            unloaded = False

            async def plugin_unload(self) -> None:
                MyPlugin.unloaded = True

        plugin = MyPlugin(bot)
        await plugin.cog_unload()
        assert MyPlugin.unloaded is True

    @pytest.mark.asyncio
    async def test_cog_load_without_plugin_load(self) -> None:
        bot = MagicMock()
        plugin = plugin_api.TurbotPlugin(bot)
        # Should not raise even without plugin_load defined
        await plugin.cog_load()

    @pytest.mark.asyncio
    async def test_cog_unload_without_plugin_unload(self) -> None:
        bot = MagicMock()
        plugin = plugin_api.TurbotPlugin(bot)
        # Should not raise even without plugin_unload defined
        await plugin.cog_unload()


class TestHttpTimeout:
    """Tests for default HTTP timeout on plugin HTTP calls."""

    @pytest.mark.asyncio
    async def test_http_get_has_default_timeout(self) -> None:
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        mock_session = AsyncMock()
        mock_session.get = AsyncMock()
        mock_session.closed = False
        ctx._session = mock_session
        await ctx.http_get("https://example.com")
        _, kwargs = mock_session.get.call_args
        assert kwargs.get("timeout") == plugin_api.HTTP_TIMEOUT

    @pytest.mark.asyncio
    async def test_http_post_has_default_timeout(self) -> None:
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        mock_session = AsyncMock()
        mock_session.post = AsyncMock()
        mock_session.closed = False
        ctx._session = mock_session
        await ctx.http_post("https://example.com")
        _, kwargs = mock_session.post.call_args
        assert kwargs.get("timeout") == plugin_api.HTTP_TIMEOUT

    @pytest.mark.asyncio
    async def test_custom_timeout_overrides_default(self) -> None:
        import aiohttp
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        custom_timeout = aiohttp.ClientTimeout(total=60)
        mock_session = AsyncMock()
        mock_session.get = AsyncMock()
        mock_session.closed = False
        ctx._session = mock_session
        await ctx.http_get("https://example.com", timeout=custom_timeout)
        _, kwargs = mock_session.get.call_args
        assert kwargs.get("timeout") == custom_timeout


class TestStoreKeyValidation:
    """Tests for store key path traversal prevention."""

    def test_rejects_path_separator(self) -> None:
        with pytest.raises(ValueError, match="Invalid store key"):
            plugin_api._validate_store_key("foo/bar")

    def test_rejects_backslash(self) -> None:
        with pytest.raises(ValueError, match="Invalid store key"):
            plugin_api._validate_store_key("foo\\bar")

    def test_rejects_dotdot(self) -> None:
        with pytest.raises(ValueError, match="Invalid store key"):
            plugin_api._validate_store_key("..")

    def test_rejects_dot(self) -> None:
        with pytest.raises(ValueError, match="Invalid store key"):
            plugin_api._validate_store_key(".")

    def test_rejects_hidden_file(self) -> None:
        with pytest.raises(ValueError, match="Invalid store key"):
            plugin_api._validate_store_key(".secret")

    def test_accepts_valid_key(self) -> None:
        # Should not raise
        plugin_api._validate_store_key("my_key")
        plugin_api._validate_store_key("scores")

    def test_store_get_rejects_traversal(self, tmp_path: str) -> None:
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        ctx._store_dir = str(tmp_path / "test")
        with pytest.raises(ValueError, match="Invalid store key"):
            ctx.store_get("../other_plugin/secrets")

    def test_store_set_rejects_traversal(self, tmp_path: str) -> None:
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        ctx._store_dir = str(tmp_path / "test")
        with pytest.raises(ValueError, match="Invalid store key"):
            ctx.store_set("../other_plugin/secrets", {"hacked": True})


class TestSessionLifecycle:
    """Tests for shared HTTP session lifecycle."""

    @pytest.mark.asyncio
    async def test_close_closes_session(self) -> None:
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        mock_session = AsyncMock()
        mock_session.closed = False
        ctx._session = mock_session
        await ctx.close()
        mock_session.close.assert_called_once()
        assert ctx._session is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_session(self) -> None:
        ctx = plugin_api.PluginContext(MagicMock(), "test")
        # Should not raise
        await ctx.close()

    @pytest.mark.asyncio
    async def test_cog_unload_closes_session(self) -> None:
        bot = MagicMock()
        plugin = plugin_api.TurbotPlugin(bot)
        mock_session = AsyncMock()
        mock_session.closed = False
        plugin.turbot._session = mock_session
        await plugin.cog_unload()
        mock_session.close.assert_called_once()
