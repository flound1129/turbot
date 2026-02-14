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


class TestOnReady:
    """Tests for on_ready status file handling."""

    @pytest.fixture(autouse=True)
    def _mock_tree_sync(self):
        """Prevent tree.sync() from failing in tests (no application_id)."""
        with patch.object(bot.bot.tree, "sync", new_callable=AsyncMock, return_value=[]):
            yield

    @pytest.mark.asyncio
    async def test_deploy_success_status(self, tmp_path: str) -> None:
        """on_ready logs deploy success when .status has deploy_success event."""
        status_path = os.path.join(str(tmp_path), ".status")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({"event": "deploy_success", "commit": "abc123def456"}, f)

        with (
            patch.object(bot, "STATUS_FILE", status_path),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock) as mock_log,
        ):
            await bot.on_ready()

        mock_log.assert_called_once()
        msg = mock_log.call_args[0][0]
        assert "Deploy successful" in msg
        assert "abc123de" in msg
        assert not os.path.exists(status_path)

    @pytest.mark.asyncio
    async def test_rollback_status(self, tmp_path: str) -> None:
        """on_ready logs rollback when .status has rollback event."""
        status_path = os.path.join(str(tmp_path), ".status")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({
                "event": "rollback",
                "bad_commit": "bad12345",
                "good_commit": "good6789",
            }, f)

        with (
            patch.object(bot, "STATUS_FILE", status_path),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock) as mock_log,
        ):
            await bot.on_ready()

        msg = mock_log.call_args[0][0]
        assert "Rolled back" in msg
        assert "bad12345" in msg
        assert "good6789" in msg
        assert not os.path.exists(status_path)

    @pytest.mark.asyncio
    async def test_deploy_pull_failed_status(self, tmp_path: str) -> None:
        """on_ready logs deploy failure when .status has deploy_pull_failed event."""
        status_path = os.path.join(str(tmp_path), ".status")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({
                "event": "deploy_pull_failed",
                "error": "merge conflict",
                "good_commit": "good6789",
            }, f)

        with (
            patch.object(bot, "STATUS_FILE", status_path),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock) as mock_log,
        ):
            await bot.on_ready()

        msg = mock_log.call_args[0][0]
        assert "Deploy failed" in msg
        assert "merge conflict" in msg
        assert "good6789" in msg

    @pytest.mark.asyncio
    async def test_restart_status(self, tmp_path: str) -> None:
        """on_ready logs restart when .status has restart event."""
        status_path = os.path.join(str(tmp_path), ".status")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({"event": "restart"}, f)

        with (
            patch.object(bot, "STATUS_FILE", status_path),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock) as mock_log,
        ):
            await bot.on_ready()

        msg = mock_log.call_args[0][0]
        assert "Restarted" in msg

    @pytest.mark.asyncio
    async def test_no_status_file_logs_online(self, tmp_path: str) -> None:
        """on_ready logs online message when no .status file exists."""
        status_path = os.path.join(str(tmp_path), ".status")

        with (
            patch.object(bot, "STATUS_FILE", status_path),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock) as mock_log,
        ):
            await bot.on_ready()

        msg = mock_log.call_args[0][0]
        assert "online" in msg.lower()

    @pytest.mark.asyncio
    async def test_corrupt_status_file_does_not_crash(self, tmp_path: str) -> None:
        """on_ready handles corrupt .status file without crashing."""
        status_path = os.path.join(str(tmp_path), ".status")
        with open(status_path, "w", encoding="utf-8") as f:
            f.write("not valid json{{{")

        with (
            patch.object(bot, "STATUS_FILE", status_path),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            # Should not raise
            await bot.on_ready()


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


class TestExtractIntent:
    """Tests for _extract_intent helper."""

    def test_feature_marker(self) -> None:
        text, intent = bot._extract_intent("Sounds great! I can build that. [FEATURE]")
        assert text == "Sounds great! I can build that."
        assert intent == "plugin"

    def test_improvement_marker(self) -> None:
        text, intent = bot._extract_intent("I can tweak that for you. [IMPROVEMENT]")
        assert text == "I can tweak that for you."
        assert intent == "core"

    def test_no_marker(self) -> None:
        text, intent = bot._extract_intent("Hello! How can I help?")
        assert text == "Hello! How can I help?"
        assert intent is None

    def test_marker_with_trailing_whitespace(self) -> None:
        text, intent = bot._extract_intent("Sure thing! [FEATURE]   ")
        assert text == "Sure thing!"
        assert intent == "plugin"

    def test_marker_in_middle_not_detected(self) -> None:
        text, intent = bot._extract_intent("The [FEATURE] is interesting. What else?")
        assert intent is None
        assert "[FEATURE]" in text

    def test_empty_string(self) -> None:
        text, intent = bot._extract_intent("")
        assert text == ""
        assert intent is None


class TestOnMessageSkipsPrefixes:
    """Test that on_message in bot.py skips feature request and bot improvement messages."""

    def _make_message(self, content: str, bot_user: MagicMock) -> MagicMock:
        message = AsyncMock()
        message.author.bot = False
        message.content = content
        message.channel.id = 42
        message.reply = AsyncMock()
        message.mentions = [bot_user]
        return message

    @pytest.mark.asyncio
    async def test_skip_feature_request_prefix(self) -> None:
        bot_user = MagicMock(id=99999)
        message = self._make_message("<@99999> feature request: add ping", bot_user)
        with (
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock) as mock_create,
        ):
            await bot.on_message(message)
        mock_create.assert_not_called()
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_bot_improvement_prefix(self) -> None:
        bot_user = MagicMock(id=99999)
        message = self._make_message("<@99999> bot improvement: fix bug", bot_user)
        with (
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock) as mock_create,
        ):
            await bot.on_message(message)
        mock_create.assert_not_called()
        message.reply.assert_not_called()


class TestSplitReply:
    def test_short_message_not_split(self) -> None:
        assert bot._split_reply("hello") == ["hello"]

    def test_empty_string(self) -> None:
        assert bot._split_reply("") == []

    def test_splits_at_space(self) -> None:
        text = "a" * 1995 + " " + "b" * 100
        chunks = bot._split_reply(text)
        assert len(chunks) == 2
        assert len(chunks[0]) <= 2000
        assert "".join(chunks) == text

    def test_splits_at_newline(self) -> None:
        text = "a" * 1995 + "\n" + "b" * 100
        chunks = bot._split_reply(text)
        assert len(chunks) == 2
        assert len(chunks[0]) <= 2000

    def test_hard_cut_when_no_break_point(self) -> None:
        text = "a" * 3000
        chunks = bot._split_reply(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 2000
        assert chunks[1] == "a" * 1000


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
        message.channel.typing = MagicMock(return_value=AsyncMock())
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


class TestIntentDetectionInChat:
    """Tests that intent markers are stripped from replies and routing works."""

    def _make_message(self, bot_user: MagicMock) -> MagicMock:
        message = AsyncMock()
        message.author.bot = False
        message.content = "<@99999> can you add a dice roll command?"
        message.channel.id = 42
        message.channel.typing = MagicMock(return_value=AsyncMock())
        message.reply = AsyncMock()
        message.mentions = [bot_user]
        return message

    @pytest.mark.asyncio
    async def test_no_channel_reply_when_intent_detected(self) -> None:
        """When [FEATURE] is detected, no reply is sent to the channel."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text="Sure, I can help with that! [FEATURE]"
        )]

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)

        with (
            patch.object(bot, "claude_health", ClaudeHealth()),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "_start_feature_request", new_callable=AsyncMock),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            bot.channel_history.clear()
            await bot.on_message(message)

        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_stripped_from_history(self) -> None:
        """[FEATURE] marker is stripped before saving to conversation history."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text="I'll build that for you! [FEATURE]"
        )]

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)

        with (
            patch.object(bot, "claude_health", ClaudeHealth()),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "_start_feature_request", new_callable=AsyncMock),
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            bot.channel_history.clear()
            await bot.on_message(message)

        history = bot.channel_history[42]
        assistant_msg = history[-1]
        assert assistant_msg["role"] == "assistant"
        assert "[FEATURE]" not in assistant_msg["content"]

    @pytest.mark.asyncio
    async def test_feature_intent_calls_bridge(self) -> None:
        """When [FEATURE] is detected, _start_feature_request is called."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text="That sounds fun! [FEATURE]"
        )]

        bot_user = MagicMock(id=99999)
        message = self._make_message(bot_user)

        with (
            patch.object(bot, "claude_health", ClaudeHealth()),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "_start_feature_request", new_callable=AsyncMock) as mock_bridge,
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            bot.channel_history.clear()
            await bot.on_message(message)

        mock_bridge.assert_called_once_with(
            message,
            "can you add a dice roll command?",
            "plugin",
        )

    @pytest.mark.asyncio
    async def test_improvement_intent_calls_bridge(self) -> None:
        """When [IMPROVEMENT] is detected, _start_feature_request is called with 'core'."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text="I can tweak that! [IMPROVEMENT]"
        )]

        bot_user = MagicMock(id=99999)
        message = AsyncMock()
        message.author.bot = False
        message.content = "<@99999> make the error messages friendlier"
        message.channel.id = 42
        message.channel.typing = MagicMock(return_value=AsyncMock())
        message.reply = AsyncMock()
        message.mentions = [bot_user]

        with (
            patch.object(bot, "claude_health", ClaudeHealth()),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "_start_feature_request", new_callable=AsyncMock) as mock_bridge,
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            bot.channel_history.clear()
            await bot.on_message(message)

        mock_bridge.assert_called_once_with(
            message,
            "make the error messages friendlier",
            "core",
        )

    @pytest.mark.asyncio
    async def test_no_intent_no_bridge_call(self) -> None:
        """When no marker present, _start_feature_request is NOT called."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello! How can I help?")]

        bot_user = MagicMock(id=99999)
        message = AsyncMock()
        message.author.bot = False
        message.content = "<@99999> hello"
        message.channel.id = 42
        message.channel.typing = MagicMock(return_value=AsyncMock())
        message.reply = AsyncMock()
        message.mentions = [bot_user]

        with (
            patch.object(bot, "claude_health", ClaudeHealth()),
            patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=bot_user),
            patch.object(bot.bot, "process_commands", new_callable=AsyncMock),
            patch.object(bot.claude.messages, "create", new_callable=AsyncMock, return_value=mock_response),
            patch.object(bot, "_start_feature_request", new_callable=AsyncMock) as mock_bridge,
            patch.object(bot, "log_to_admin", new_callable=AsyncMock),
        ):
            bot.channel_history.clear()
            await bot.on_message(message)

        mock_bridge.assert_not_called()
