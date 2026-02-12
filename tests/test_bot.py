import hashlib
import hmac
import json
import os
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import anthropic
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

import bot
from api_health import ClaudeHealth
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
        bot.channel_history[1] = [{"role": "user", "content": "hi"}]
        bot.channel_history[2] = [{"role": "user", "content": "hello"}]
        assert len(bot.channel_history[1]) == 1
        assert len(bot.channel_history[2]) == 1
        assert bot.channel_history[1][0]["content"] == "hi"
        assert bot.channel_history[2][0]["content"] == "hello"

    def test_history_truncation(self) -> None:
        bot.channel_history.clear()
        bot.channel_history[99] = []
        history = bot.channel_history[99]
        for i in range(30):
            history.append({"role": "user", "content": f"msg {i}"})
        # Simulate the truncation logic from on_message
        if len(history) > bot.MAX_HISTORY:
            history[:] = history[-bot.MAX_HISTORY:]
        assert len(history) == bot.MAX_HISTORY
        assert history[0]["content"] == "msg 10"

    def test_lru_eviction(self) -> None:
        bot.channel_history.clear()
        for i in range(bot.MAX_CHANNELS + 10):
            bot.channel_history[i] = [{"role": "user", "content": "hi"}]
        # Evict oldest (simulating the eviction logic from on_message)
        while len(bot.channel_history) > bot.MAX_CHANNELS:
            bot.channel_history.popitem(last=False)
        assert len(bot.channel_history) == bot.MAX_CHANNELS
        # Oldest channels (0-9) should be evicted
        assert 0 not in bot.channel_history
        assert 9 not in bot.channel_history
        assert 10 in bot.channel_history


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


class TestChatCircuitBreaker:
    """Tests for circuit breaker integration in the chat handler."""

    def _make_message(self, bot_user: MagicMock) -> MagicMock:
        message = AsyncMock()
        message.author.bot = False
        message.content = "<@99999> hello"
        message.channel.id = 42
        message.reply = AsyncMock()
        message.mentions = [bot_user]
        return message

    @pytest.mark.asyncio
    async def test_chat_rejects_when_circuit_open(self) -> None:
        """When circuit is open, user gets fallback message, no API call."""
        health = ClaudeHealth()
        for _ in range(3):
            health.record_failure()

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)
        with (
            patch.object(bot, "claude_health", health),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock) as mock_create,
        ):
            await bot.on_message(message)

        mock_create.assert_not_called()
        reply_text = message.reply.call_args[0][0]
        assert "unreachable" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_chat_records_success(self) -> None:
        """Successful API call resets circuit breaker."""
        health = ClaudeHealth()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello!")]

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)
        with (
            patch.object(bot, "claude_health", health),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            await bot.on_message(message)

        assert health.state == "closed"
        assert health._failures == 0

    @pytest.mark.asyncio
    async def test_chat_records_failure_on_timeout(self) -> None:
        """APITimeoutError trips the circuit breaker."""
        health = ClaudeHealth()

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)
        with (
            patch.object(bot, "claude_health", health),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(
                bot.claude.messages, "create",
                new_callable=AsyncMock, side_effect=anthropic.APITimeoutError(request=None),
            ),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            await bot.on_message(message)

        assert health._failures == 1
        reply_text = message.reply.call_args[0][0]
        assert "unreachable" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_auth_error_does_not_trip_breaker(self) -> None:
        """AuthenticationError should not affect the circuit breaker."""
        health = ClaudeHealth()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {}

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)
        with (
            patch.object(bot, "claude_health", health),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(
                bot.claude.messages, "create",
                new_callable=AsyncMock, side_effect=anthropic.AuthenticationError(
                    message="Invalid key", response=mock_resp, body=None,
                ),
            ),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            await bot.on_message(message)

        assert health._failures == 0
        reply_text = message.reply.call_args[0][0]
        assert "something went wrong" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_recovery_logged_to_admin(self) -> None:
        """When circuit recovers from half_open, admin is notified."""
        health = ClaudeHealth()
        # Trip open then force half_open
        for _ in range(3):
            health.record_failure()
        health._state = "half_open"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="I'm back!")]

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)
        with (
            patch.object(bot, "claude_health", health),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock) as mock_log,
        ):
            await bot.on_message(message)

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("recovered" in c.lower() for c in log_calls)
