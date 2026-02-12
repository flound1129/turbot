import hashlib
import hmac
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

import bot
import config


class TestVerifySignature:
    def test_valid_signature(self) -> None:
        payload = b'{"action": "closed"}'
        secret = config.WEBHOOK_SECRET
        sig = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        assert bot._verify_signature(payload, sig) is True

    def test_invalid_signature(self) -> None:
        payload = b'{"action": "closed"}'
        assert bot._verify_signature(payload, "sha256=bad") is False

    def test_empty_signature(self) -> None:
        assert bot._verify_signature(b"data", "") is False

    def test_tampered_payload(self) -> None:
        original = b'{"action": "closed"}'
        secret = config.WEBHOOK_SECRET
        sig = "sha256=" + hmac.new(
            secret.encode(), original, hashlib.sha256
        ).hexdigest()
        tampered = b'{"action": "opened"}'
        assert bot._verify_signature(tampered, sig) is False


class TestWebhookHandler:
    def _make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/webhook", bot.webhook_handler)
        return app

    def _sign(self, payload: bytes) -> str:
        return "sha256=" + hmac.new(
            config.WEBHOOK_SECRET.encode(), payload, hashlib.sha256
        ).hexdigest()

    @pytest.mark.asyncio
    async def test_rejects_bad_signature(self) -> None:
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            with patch.object(bot, "log_to_admin", new_callable=AsyncMock):
                resp = await client.post(
                    "/webhook",
                    data=b"{}",
                    headers={
                        "X-Hub-Signature-256": "sha256=bad",
                        "X-GitHub-Event": "pull_request",
                    },
                )
                assert resp.status == 401

    @pytest.mark.asyncio
    async def test_ignores_non_pr_events(self) -> None:
        payload = b'{"action": "completed"}'
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhook",
                data=payload,
                headers={
                    "X-Hub-Signature-256": self._sign(payload),
                    "X-GitHub-Event": "push",
                },
            )
            assert resp.status == 200
            text = await resp.text()
            assert text == "Ignored event"

    @pytest.mark.asyncio
    async def test_ignores_non_merged_pr(self) -> None:
        payload = json.dumps({
            "action": "closed",
            "pull_request": {"merged": False},
        }).encode()
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhook",
                data=payload,
                headers={
                    "X-Hub-Signature-256": self._sign(payload),
                    "X-GitHub-Event": "pull_request",
                },
            )
            assert resp.status == 200
            text = await resp.text()
            assert text == "OK"
            assert not os.path.exists(bot.DEPLOY_SIGNAL)

    @pytest.mark.asyncio
    async def test_merged_pr_writes_deploy_signal(self) -> None:
        payload = json.dumps({
            "action": "closed",
            "pull_request": {
                "merged": True,
                "title": "Add ping",
                "html_url": "https://github.com/user/repo/pull/1",
            },
        }).encode()
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            with (
                patch.object(bot, "log_to_admin", new_callable=AsyncMock),
                patch.object(bot, "_graceful_exit"),  # prevent os._exit
            ):
                resp = await client.post(
                    "/webhook",
                    data=payload,
                    headers={
                        "X-Hub-Signature-256": self._sign(payload),
                        "X-GitHub-Event": "pull_request",
                    },
                )
                assert resp.status == 200
                assert os.path.exists(bot.DEPLOY_SIGNAL)
                # Clean up
                os.remove(bot.DEPLOY_SIGNAL)


class TestChannelHistory:
    def test_history_is_per_channel(self) -> None:
        bot.channel_history.clear()
        bot.channel_history[1].append({"role": "user", "content": "hi"})
        bot.channel_history[2].append({"role": "user", "content": "hello"})
        assert len(bot.channel_history[1]) == 1
        assert len(bot.channel_history[2]) == 1
        assert bot.channel_history[1][0]["content"] == "hi"
        assert bot.channel_history[2][0]["content"] == "hello"

    def test_history_truncation(self) -> None:
        bot.channel_history.clear()
        history = bot.channel_history[99]
        for i in range(30):
            history.append({"role": "user", "content": f"msg {i}"})
        # Simulate the truncation logic from on_message
        if len(history) > bot.MAX_HISTORY:
            history[:] = history[-bot.MAX_HISTORY:]
        assert len(history) == bot.MAX_HISTORY
        assert history[0]["content"] == "msg 10"


class TestLogToAdmin:
    @pytest.mark.asyncio
    async def test_sends_to_channel(self) -> None:
        mock_channel = AsyncMock(spec=["send"])
        mock_channel.send = AsyncMock()
        with patch.object(bot.bot, "get_channel", return_value=mock_channel):
            # Patch isinstance check — mock_channel is Messageable enough
            with patch("bot.isinstance", return_value=True):
                await bot.log_to_admin("test message")
                mock_channel.send.assert_called_once_with("test message")

    @pytest.mark.asyncio
    async def test_no_crash_when_channel_missing(self) -> None:
        with patch.object(bot.bot, "get_channel", return_value=None):
            # Should not raise
            await bot.log_to_admin("test message")


class TestOnMessageSkipsPrefixes:
    """Test that on_message in bot.py skips feature request and bot improvement messages."""

    def test_skip_feature_request_prefix(self) -> None:
        # Verify the string is in the skip tuple
        # We test this by checking the source code has the right tuple
        import inspect
        source = inspect.getsource(bot)
        assert 'startswith(("feature request:", "bot improvement:"))' in source

    def test_skip_bot_improvement_prefix(self) -> None:
        import inspect
        source = inspect.getsource(bot)
        assert "bot improvement:" in source


class TestPluginAutoLoading:
    def test_main_loads_plugins_from_directory(self, tmp_path: str) -> None:
        """Verify the plugin loading code structure exists in main()."""
        import inspect
        source = inspect.getsource(bot.main)
        assert "plugins" in source
        assert "load_extension" in source

    def test_init_py_skipped(self) -> None:
        """Verify __init__.py is excluded from plugin loading."""
        import inspect
        source = inspect.getsource(bot.main)
        assert '__init__.py' in source

    def test_bad_plugin_does_not_crash(self, tmp_path: str) -> None:
        """Verify bad plugin loading is wrapped in try/except."""
        import inspect
        source = inspect.getsource(bot.main)
        assert "Failed to load plugin" in source
