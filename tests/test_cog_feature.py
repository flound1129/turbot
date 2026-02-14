"""Tests for the feature request cog (dual-path: plugin vs core)."""

import json
import re
import time
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import anthropic
import discord
import pytest

from api_health import ClaudeHealth
import cog_feature


def _make_author(
    *,
    is_bot: bool = False,
    user_id: int = 55555,
    has_role: bool = False,
    role_name: str = "BotAdmin",
) -> MagicMock:
    """Create a mock author with standard Discord member attributes."""
    author = MagicMock()
    author.bot = is_bot
    author.id = user_id
    if has_role:
        role = MagicMock()
        role.name = role_name
        author.roles = [role]
    else:
        author.roles = []
    return author


class TestReadProjectFiles:
    def test_reads_py_files(self, tmp_path: str) -> None:
        py_file = tmp_path / "test.py"
        py_file.write_text("print('hi')", encoding="utf-8")
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("not python", encoding="utf-8")

        with patch.object(cog_feature, "PROJECT_DIR", str(tmp_path)):
            files = cog_feature._read_project_files()
            assert "test.py" in files
            assert files["test.py"] == "print('hi')"
            assert "readme.txt" not in files

    def test_empty_directory(self, tmp_path: str) -> None:
        with patch.object(cog_feature, "PROJECT_DIR", str(tmp_path)):
            files = cog_feature._read_project_files()
            assert files == {}


class TestReadPluginContext:
    def test_reads_plugin_api(self, tmp_path: str) -> None:
        api_file = tmp_path / "plugin_api.py"
        api_file.write_text("class TurbotPlugin: pass", encoding="utf-8")

        with patch.object(cog_feature, "PROJECT_DIR", str(tmp_path)):
            files = cog_feature._read_plugin_context()
            assert "plugin_api.py" in files

    def test_reads_existing_plugins(self, tmp_path: str) -> None:
        api_file = tmp_path / "plugin_api.py"
        api_file.write_text("class TurbotPlugin: pass", encoding="utf-8")
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugins_dir / "ping.py").write_text("# ping", encoding="utf-8")

        with patch.object(cog_feature, "PROJECT_DIR", str(tmp_path)):
            files = cog_feature._read_plugin_context()
            assert "plugins/ping.py" in files
            assert "__init__.py" not in str(files.keys())

    def test_does_not_read_core_files(self, tmp_path: str) -> None:
        api_file = tmp_path / "plugin_api.py"
        api_file.write_text("api", encoding="utf-8")
        (tmp_path / "bot.py").write_text("secret stuff", encoding="utf-8")
        (tmp_path / "config.py").write_text("tokens", encoding="utf-8")

        with patch.object(cog_feature, "PROJECT_DIR", str(tmp_path)):
            files = cog_feature._read_plugin_context()
            assert "bot.py" not in files
            assert "config.py" not in files


class TestDetectRequestType:
    def test_feature_request(self) -> None:
        assert cog_feature._detect_request_type("feature request: add ping") == "plugin"

    def test_bot_improvement(self) -> None:
        assert cog_feature._detect_request_type("bot improvement: fix bug") == "core"

    def test_case_insensitive(self) -> None:
        assert cog_feature._detect_request_type("Feature Request: add ping") == "plugin"
        assert cog_feature._detect_request_type("BOT IMPROVEMENT: fix bug") == "core"

    def test_neither(self) -> None:
        assert cog_feature._detect_request_type("hello there!") is None


class TestFeatureRequestCog:
    def _make_message(
        self,
        content: str,
        *,
        is_bot: bool = False,
        has_role: bool = False,
        role_name: str = "BotAdmin",
        mentioned: bool = True,
    ) -> tuple[MagicMock, MagicMock]:
        message = AsyncMock()
        message.content = content
        message.channel = MagicMock()  # Plain mock — NOT a discord.Thread
        message.channel.id = 12345
        message.reply = AsyncMock()
        message.author = _make_author(
            is_bot=is_bot, has_role=has_role, role_name=role_name,
        )

        # Mock thread creation
        mock_thread = AsyncMock()
        mock_thread.id = 99900
        mock_thread.send = AsyncMock()
        message.create_thread = AsyncMock(return_value=mock_thread)

        bot_user = MagicMock()
        bot_user.id = 99999

        if mentioned:
            message.mentions = [bot_user]
        else:
            message.mentions = []

        return message, bot_user

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self) -> None:
        message, bot_user = self._make_message(
            "<@99999> feature request: add ping",
            is_bot=True,
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)
        await cog.on_message(message)
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_mentions(self) -> None:
        message, bot_user = self._make_message(
            "feature request: add ping",
            mentioned=False,
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)
        await cog.on_message(message)
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_feature_requests(self) -> None:
        message, bot_user = self._make_message(
            "<@99999> hello there!",
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)
        await cog.on_message(message)
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_without_role(self) -> None:
        message, bot_user = self._make_message(
            "<@99999> feature request: add ping",
            has_role=False,
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        await cog.on_message(message)

        message.reply.assert_called_once()
        reply_text = message.reply.call_args[0][0]
        assert "BotAdmin" in reply_text

    @pytest.mark.asyncio
    async def test_rejects_empty_description(self) -> None:
        message, bot_user = self._make_message(
            "<@99999> feature request:",
            has_role=True,
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        await cog.on_message(message)

        calls = [str(c) for c in message.reply.call_args_list]
        assert any("describe the feature" in c.lower() for c in calls)

    @pytest.mark.asyncio
    async def test_successful_plugin_request_creates_thread(self) -> None:
        """Feature request creates a thread and starts planning conversation."""
        message, bot_user = self._make_message(
            "<@99999> feature request: add a ping command",
            has_role=True,
            role_name="BotAdmin",
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        planning_response = MagicMock()
        planning_response.content = [MagicMock(
            text="Interesting! A few questions:\n1. What should !ping respond with?"
        )]

        sessions_dict: dict = {}
        with (
            patch.object(cog_feature, "_last_request", {}),
            patch.object(cog_feature, "_sessions", sessions_dict),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, return_value=planning_response,
            ),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

            # Build diagnostic info for CI debugging
            diag = (
                f"author.bot={message.author.bot!r} "
                f"type(author.bot)={type(message.author.bot).__name__} "
                f"bool(author.bot)={bool(message.author.bot)} "
                f"channel.id={message.channel.id!r} "
                f"bot.user={cog.bot.user!r} "
                f"mentions={message.mentions!r} "
                f"bot_user_in_mentions={cog.bot.user in message.mentions} "
                f"content={message.content!r} "
                f"reply_calls={message.reply.call_args_list!r} "
                f"_sessions={cog_feature._sessions!r} "
                f"_last_request={cog_feature._last_request!r}"
            )

            # Thread should have been created
            assert message.create_thread.called, f"create_thread not called. {diag}"
            thread_name = message.create_thread.call_args[1]["name"]
            assert "add a ping command" in thread_name

            # Planning response should have been sent to thread
            mock_thread = message.create_thread.return_value
            mock_thread.send.assert_called_once()
            send_text = mock_thread.send.call_args[0][0]
            assert "questions" in send_text.lower()

            # Session should be tracked
            assert 99900 in sessions_dict, f"session not in dict: {sessions_dict}"
            session = sessions_dict[99900]
            assert session.state == "discussing"
            assert session.request_type == "plugin"

    @pytest.mark.asyncio
    async def test_successful_core_request_creates_thread(self) -> None:
        """Bot improvement request creates a thread and starts planning."""
        message, bot_user = self._make_message(
            "<@99999> bot improvement: fix a bug in bot.py",
            has_role=True,
            role_name="BotAdmin",
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        planning_response = MagicMock()
        planning_response.content = [MagicMock(
            text="I can help with that! What bug are you seeing?"
        )]

        sessions_dict: dict = {}
        with (
            patch.object(cog_feature, "_last_request", {}),
            patch.object(cog_feature, "_sessions", sessions_dict),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, return_value=planning_response,
            ),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

            diag = (
                f"author.bot={message.author.bot!r} "
                f"bool(author.bot)={bool(message.author.bot)} "
                f"bot_user_in_mentions={cog.bot.user in message.mentions} "
                f"content={message.content!r} "
                f"reply_calls={message.reply.call_args_list!r}"
            )
            assert message.create_thread.called, f"create_thread not called. {diag}"
            assert 99900 in sessions_dict, f"session not in dict: {sessions_dict}"
            session = sessions_dict[99900]
            assert session.request_type == "core"


class TestPolicyViolationRejectspr:
    @pytest.mark.asyncio
    async def test_policy_violations_reject_pr(self) -> None:
        """Plugin request with policy violations should not create a PR."""
        mock_bot = MagicMock()
        mock_bot.user = MagicMock(id=99999)
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({
            "changes": [{
                "path": "plugins/evil.py",
                "action": "create",
                "content": "import os\nexec('bad')\n",
            }],
            "summary": "Evil plugin",
            "title": "Evil",
        }))]

        with (
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_plugin_context", return_value={}),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch("github_ops.create_branch", new_callable=AsyncMock) as mock_branch,
        ):
            with pytest.raises(ValueError, match="Security policy violations"):
                await cog._handle_request("add evil thing", "plugin")


class TestCoreChangeFlag:
    @pytest.mark.asyncio
    async def test_core_change_flagged_in_pr_body(self) -> None:
        """Changes to files outside plugins/ should get CORE CHANGE flag."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({
            "changes": [{
                "path": "bot.py",
                "action": "modify",
                "content": "# changed",
            }],
            "summary": "Modified core file",
            "title": "Core change",
        }))]

        with (
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_project_files", return_value={"bot.py": "# bot"}),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch("github_ops.create_branch", new_callable=AsyncMock, return_value="feature/core"),
            patch("github_ops.apply_changes"),
            patch("github_ops.commit_and_push", new_callable=AsyncMock),
            patch("github_ops.open_pr", new_callable=AsyncMock, return_value="https://github.com/pr/1") as mock_pr,
            patch("github_ops._run", new_callable=AsyncMock),
        ):
            await cog._handle_request("fix something", "core")

        # Check that the PR body contains CORE CHANGE
        pr_call_args = mock_pr.call_args
        pr_body = pr_call_args[0][2]  # third positional arg is body
        assert "CORE CHANGE" in pr_body


class TestSecurityPolicyInPrompts:
    def test_plugin_prompt_includes_policy_placeholder(self) -> None:
        assert "{security_policy}" in cog_feature.PLUGIN_SYSTEM_PROMPT

    def test_core_prompt_includes_policy_placeholder(self) -> None:
        assert "{security_policy}" in cog_feature.CORE_SYSTEM_PROMPT

    def test_plugin_prompt_mentions_plugins_directory(self) -> None:
        assert "plugins/" in cog_feature.PLUGIN_SYSTEM_PROMPT

    def test_core_prompt_mentions_core_change(self) -> None:
        assert "CORE CHANGE" in cog_feature.CORE_SYSTEM_PROMPT


class TestSystemPrompt:
    def test_prompt_mentions_turbot(self) -> None:
        assert "Turbot" in cog_feature.SYSTEM_PROMPT_BASE

    def test_prompt_requests_json(self) -> None:
        assert "JSON" in cog_feature.SYSTEM_PROMPT_BASE

    def test_prompt_defines_actions(self) -> None:
        assert "create" in cog_feature.SYSTEM_PROMPT_BASE
        assert "modify" in cog_feature.SYSTEM_PROMPT_BASE
        assert "delete" in cog_feature.SYSTEM_PROMPT_BASE


class TestFormatViolations:
    def test_format_violations_output(self) -> None:
        results = [
            cog_feature.policy.ScanResult(
                path="plugins/bad.py",
                violations=[
                    cog_feature.policy.Violation(1, 0, "banned-import", "Import of 'os' is forbidden"),
                    cog_feature.policy.Violation(2, 0, "banned-builtin", "Call to 'exec()' is forbidden"),
                ],
            ),
        ]
        output = cog_feature._format_violations(results)
        assert "Security policy violations" in output
        assert "plugins/bad.py" in output
        assert "banned-import" in output
        assert "banned-builtin" in output


class TestRateLimiting:
    """Tests for per-user request cooldown."""

    def _make_message(self) -> tuple[MagicMock, MagicMock]:
        message = AsyncMock()
        message.content = "<@99999> feature request: add a joke command"
        message.channel = MagicMock()
        message.channel.id = 12345
        message.reply = AsyncMock()
        message.author = _make_author(user_id=77777, has_role=True)

        mock_thread = AsyncMock()
        mock_thread.id = 99900
        mock_thread.send = AsyncMock()
        message.create_thread = AsyncMock(return_value=mock_thread)

        bot_user = MagicMock()
        bot_user.id = 99999
        message.mentions = [bot_user]

        return message, bot_user

    @pytest.mark.asyncio
    async def test_rejects_request_within_cooldown(self) -> None:
        """Second request within cooldown is rejected."""
        cog_feature._last_request.clear()
        message, bot_user = self._make_message()
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        # Simulate a recent request from this user
        cog_feature._last_request[77777] = time.monotonic()

        with patch.object(cog.client.messages, "create", new_callable=AsyncMock) as mock_create:
            await cog.on_message(message)

        mock_create.assert_not_called()
        reply_calls = [str(c) for c in message.reply.call_args_list]
        assert any("wait" in c.lower() for c in reply_calls)
        cog_feature._last_request.clear()

    @pytest.mark.asyncio
    async def test_allows_request_after_cooldown(self) -> None:
        """Request after cooldown period is allowed — creates thread."""
        cog_feature._last_request.clear()
        cog_feature._sessions.clear()
        message, bot_user = self._make_message()
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        # Simulate a request from long ago
        cog_feature._last_request[77777] = time.monotonic() - 999

        planning_response = MagicMock()
        planning_response.content = [MagicMock(text="Let me evaluate this request.")]

        with (
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, return_value=planning_response,
            ),
        ):
            await cog.on_message(message)

        # Thread should have been created (request was allowed)
        message.create_thread.assert_called_once()
        cog_feature._last_request.clear()
        cog_feature._sessions.clear()


class TestHandleRequestValidation:
    """Tests for Claude response validation in _handle_request."""

    @pytest.mark.asyncio
    async def test_invalid_json_raises_value_error(self) -> None:
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text="not valid json at all")]

        with (
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_plugin_context", return_value={}),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
        ):
            with pytest.raises(ValueError, match="invalid JSON"):
                await cog._handle_request("add something", "plugin")

    @pytest.mark.asyncio
    async def test_missing_changes_key_raises_value_error(self) -> None:
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({"summary": "no changes key"}))]

        with (
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_plugin_context", return_value={}),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
        ):
            with pytest.raises(ValueError, match="unexpected response"):
                await cog._handle_request("add something", "plugin")


class TestGitCleanup:
    """Tests for git state cleanup on failure."""

    @pytest.mark.asyncio
    async def test_git_checkout_on_push_failure(self) -> None:
        """Git state is cleaned up if commit_and_push fails."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({
            "changes": [{"path": "plugins/test.py", "action": "create", "content": "import json\n"}],
            "summary": "Test",
            "title": "Test",
        }))]

        with (
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_plugin_context", return_value={}),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch("github_ops.create_branch", new_callable=AsyncMock, return_value="feature/test"),
            patch("github_ops.apply_changes"),
            patch("github_ops.commit_and_push", new_callable=AsyncMock, side_effect=RuntimeError("push failed")),
            patch("github_ops._run", new_callable=AsyncMock) as mock_git_run,
        ):
            with pytest.raises(RuntimeError, match="push failed"):
                await cog._handle_request("test feature", "plugin")

        # Verify cleanup happened — git checkout -f main was called
        mock_git_run.assert_called_with(["git", "checkout", "-f", "main"])


class TestCogCircuitBreaker:
    """Tests for circuit breaker integration in FeatureRequestCog."""

    def _make_message(
        self,
        content: str = "<@99999> feature request: add a joke command",
        *,
        has_role: bool = True,
    ) -> tuple[MagicMock, MagicMock]:
        message = AsyncMock()
        message.content = content
        message.channel = MagicMock()
        message.channel.id = 12345
        message.reply = AsyncMock()
        message.author = _make_author(has_role=has_role)

        mock_thread = AsyncMock()
        mock_thread.id = 99900
        mock_thread.send = AsyncMock()
        message.create_thread = AsyncMock(return_value=mock_thread)

        bot_user = MagicMock()
        bot_user.id = 99999
        message.mentions = [bot_user]

        return message, bot_user

    @pytest.mark.asyncio
    async def test_feature_request_rejects_when_circuit_open(self) -> None:
        """When circuit is open, feature request gets clear rejection."""
        message, bot_user = self._make_message()
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        health = ClaudeHealth()
        for _ in range(3):
            health.record_failure()

        with (
            patch.object(cog_feature, "_last_request", {}),
            patch.object(cog_feature, "_sessions", {}),
            patch("cog_feature.claude_health", health),
            patch.object(cog.client.messages, "create") as mock_create,
        ):
            await cog.on_message(message)

            mock_create.assert_not_called()
            diag = (
                f"author.bot={message.author.bot!r} "
                f"bool(author.bot)={bool(message.author.bot)} "
                f"bot_user_in_mentions={cog.bot.user in message.mentions} "
                f"content={message.content!r} "
                f"reply_calls={message.reply.call_args_list!r}"
            )
            reply_calls = [str(c) for c in message.reply.call_args_list]
            assert any("unavailable" in c.lower() for c in reply_calls), (
                f"Expected 'unavailable' reply. {diag}"
            )

    @pytest.mark.asyncio
    async def test_records_success_after_code_gen(self) -> None:
        """Successful Claude call in _handle_request resets the breaker."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)
        health = ClaudeHealth()
        health.record_failure()  # one failure, still closed

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({
            "changes": [{
                "path": "plugins/joke.py",
                "action": "create",
                "content": "import json\n",
            }],
            "summary": "Added joke plugin",
            "title": "Add joke plugin",
        }))]

        with (
            patch("cog_feature.claude_health", health),
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_plugin_context", return_value={}),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
            patch("github_ops.create_branch", new_callable=AsyncMock, return_value="feature/joke"),
            patch("github_ops.apply_changes"),
            patch("github_ops.commit_and_push", new_callable=AsyncMock),
            patch("github_ops.open_pr", new_callable=AsyncMock, return_value="https://github.com/pr/1"),
            patch("github_ops._run", new_callable=AsyncMock),
        ):
            await cog._handle_request("add a joke command", "plugin")

        assert health._failures == 0
        assert health.state == "closed"

    @pytest.mark.asyncio
    async def test_records_failure_on_connectivity_error(self) -> None:
        """Connectivity error during planning triggers circuit breaker."""
        message, bot_user = self._make_message()
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        health = ClaudeHealth()

        with (
            patch.object(cog_feature, "_last_request", {}),
            patch.object(cog_feature, "_sessions", {}),
            patch("cog_feature.claude_health", health),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, side_effect=anthropic.APITimeoutError(request=None),
            ),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

            diag = (
                f"author.bot={message.author.bot!r} "
                f"bool(author.bot)={bool(message.author.bot)} "
                f"bot_user_in_mentions={cog.bot.user in message.mentions} "
                f"content={message.content!r} "
                f"reply_calls={message.reply.call_args_list!r} "
                f"create_thread_called={message.create_thread.called}"
            )
            assert health._failures == 1, f"Expected 1 failure. {diag}"
            # Error message should be sent to the thread
            mock_thread = message.create_thread.return_value
            send_calls = [str(c) for c in mock_thread.send.call_args_list]
            assert any("unavailable" in c.lower() for c in send_calls)


class TestPlanningSystemPrompt:
    """Tests for the planning system prompt."""

    def test_planning_prompt_mentions_turbot(self) -> None:
        assert "Turbot" in cog_feature.PLANNING_SYSTEM_PROMPT

    def test_planning_prompt_mentions_plan_ready_marker(self) -> None:
        assert cog_feature.PLAN_READY_MARKER in cog_feature.PLANNING_SYSTEM_PROMPT

    def test_planning_prompt_instructs_clarifying_questions(self) -> None:
        assert "clarifying" in cog_feature.PLANNING_SYSTEM_PROMPT.lower()


class TestThreadSession:
    """Tests for ThreadSession dataclass and helpers."""

    def test_session_default_state(self) -> None:
        session = cog_feature.ThreadSession(
            thread_id=1, user_id=2, request_type="plugin",
            original_description="test",
        )
        assert session.state == "discussing"
        assert session.messages == []
        assert session.refined_description is None

    def test_check_session_timeout_not_expired(self) -> None:
        session = cog_feature.ThreadSession(
            thread_id=1, user_id=2, request_type="plugin",
            original_description="test",
        )
        session.last_active = time.monotonic()
        assert not cog_feature._check_session_timeout(session)

    def test_check_session_timeout_expired(self) -> None:
        session = cog_feature.ThreadSession(
            thread_id=1, user_id=2, request_type="plugin",
            original_description="test",
        )
        session.last_active = time.monotonic() - 2000
        assert cog_feature._check_session_timeout(session)

    def test_is_confirmation(self) -> None:
        assert cog_feature._is_confirmation("go")
        assert cog_feature._is_confirmation("Go")
        assert cog_feature._is_confirmation("  yes  ")
        assert cog_feature._is_confirmation("lgtm")
        assert cog_feature._is_confirmation("ship it")
        assert not cog_feature._is_confirmation("maybe")
        assert not cog_feature._is_confirmation("add more details")

    def test_is_cancellation(self) -> None:
        assert cog_feature._is_cancellation("cancel")
        assert cog_feature._is_cancellation("Cancel")
        assert cog_feature._is_cancellation("nvm")
        assert cog_feature._is_cancellation("abort")
        assert not cog_feature._is_cancellation("go")
        assert not cog_feature._is_cancellation("hello")


def _make_thread_message(
    thread_id: int,
    user_id: int,
    content: str,
) -> MagicMock:
    """Helper to create a mock message in a tracked thread."""
    message = AsyncMock()
    message.author.bot = False
    message.author.id = user_id
    message.content = content

    # Make the channel look like a discord.Thread
    channel = MagicMock(spec=discord.Thread)
    channel.id = thread_id
    channel.send = AsyncMock()
    message.channel = channel

    return message


class TestThreadConversation:
    """Tests for the multi-turn thread conversation flow."""

    def setup_method(self) -> None:
        cog_feature._sessions.clear()
        cog_feature._last_request.clear()

    def teardown_method(self) -> None:
        cog_feature._sessions.clear()
        cog_feature._last_request.clear()

    @pytest.mark.asyncio
    async def test_thread_message_forwarded_to_claude(self) -> None:
        """User message in thread is forwarded to Claude for planning."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            messages=[
                {"role": "user", "content": "Feature request: add a leaderboard"},
                {"role": "assistant", "content": "What kind of leaderboard?"},
            ],
        )
        cog_feature._sessions[5000] = session

        planning_response = MagicMock()
        planning_response.content = [MagicMock(text="Got it, a points-based leaderboard.")]

        message = _make_thread_message(5000, 111, "A points-based leaderboard")

        with patch.object(
            cog.client.messages, "create",
            new_callable=AsyncMock, return_value=planning_response,
        ) as mock_create:
            await cog.on_message(message)

            # Claude should have been called
            mock_create.assert_called_once()

        # Response should be sent to thread
        message.channel.send.assert_called_once_with("Got it, a points-based leaderboard.")
        # User message should be added to session
        assert session.messages[-2]["role"] == "user"
        assert session.messages[-2]["content"] == "A points-based leaderboard"
        assert session.messages[-1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_thread_message_from_different_user_ignored(self) -> None:
        """Messages from non-requester in thread are ignored."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
        )
        cog_feature._sessions[5000] = session

        # Different user (222) sends message in the thread
        message = _make_thread_message(5000, 222, "I also want this!")

        with patch.object(cog.client.messages, "create", new_callable=AsyncMock) as mock_create:
            await cog.on_message(message)

        mock_create.assert_not_called()
        message.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_plan_ready_marker_detected_and_stripped(self) -> None:
        """PLAN_READY marker transitions state and is stripped from display."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            messages=[
                {"role": "user", "content": "Feature request: add a leaderboard"},
                {"role": "assistant", "content": "What kind?"},
            ],
        )
        cog_feature._sessions[5000] = session

        plan_text = "Here's the plan: create a points-based leaderboard plugin.\n---PLAN_READY---"
        planning_response = MagicMock()
        planning_response.content = [MagicMock(text=plan_text)]

        message = _make_thread_message(5000, 111, "A points-based one")

        with patch.object(
            cog.client.messages, "create",
            new_callable=AsyncMock, return_value=planning_response,
        ):
            await cog.on_message(message)

        assert session.state == "plan_ready"
        assert session.refined_description is not None
        assert "---PLAN_READY---" not in session.refined_description

        # Display text should not contain the marker but should have the go prompt
        send_text = message.channel.send.call_args[0][0]
        assert "---PLAN_READY---" not in send_text
        assert "go" in send_text.lower()

    @pytest.mark.asyncio
    async def test_confirmation_triggers_code_generation(self) -> None:
        """Saying 'go' in plan_ready state triggers code gen and PR creation."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            state="plan_ready",
            refined_description="Create a points-based leaderboard plugin",
        )
        cog_feature._sessions[5000] = session

        message = _make_thread_message(5000, 111, "go")

        with (
            patch.object(
                cog, "_handle_request",
                new_callable=AsyncMock,
                return_value="https://github.com/user/repo/pull/42",
            ) as mock_handle,
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

            # _handle_request should have been called with the refined description
            mock_handle.assert_called_once_with(
                "Create a points-based leaderboard plugin", "plugin",
            )

        # PR link should be posted in thread
        send_calls = [str(c) for c in message.channel.send.call_args_list]
        assert any("pull/42" in c for c in send_calls)
        assert any("Turbotastic" in c for c in send_calls)
        # Session should be cleaned up
        assert 5000 not in cog_feature._sessions

    @pytest.mark.asyncio
    async def test_confirmation_uses_original_desc_if_no_refined(self) -> None:
        """If no refined_description, confirmation uses original_description."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            state="plan_ready",
            refined_description=None,
        )
        cog_feature._sessions[5000] = session

        message = _make_thread_message(5000, 111, "yes")

        with (
            patch.object(
                cog, "_handle_request",
                new_callable=AsyncMock,
                return_value="https://github.com/user/repo/pull/1",
            ) as mock_handle,
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

            mock_handle.assert_called_once_with(
                "add a leaderboard", "plugin",
            )

    @pytest.mark.asyncio
    async def test_cancel_ends_session(self) -> None:
        """Saying 'cancel' in plan_ready state ends the session."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            state="plan_ready",
        )
        cog_feature._sessions[5000] = session

        message = _make_thread_message(5000, 111, "cancel")

        await cog.on_message(message)

        send_text = message.channel.send.call_args[0][0]
        assert "cancelled" in send_text.lower()
        assert 5000 not in cog_feature._sessions

    @pytest.mark.asyncio
    async def test_non_confirm_in_plan_ready_returns_to_discussing(self) -> None:
        """Unrecognized text in plan_ready returns to discussing state."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            state="plan_ready",
            messages=[
                {"role": "user", "content": "Feature request: add a leaderboard"},
                {"role": "assistant", "content": "Plan: leaderboard plugin"},
            ],
        )
        cog_feature._sessions[5000] = session

        planning_response = MagicMock()
        planning_response.content = [MagicMock(text="Updated plan with more detail.")]

        message = _make_thread_message(5000, 111, "Actually, can it also track streaks?")

        with patch.object(
            cog.client.messages, "create",
            new_callable=AsyncMock, return_value=planning_response,
        ) as mock_create:
            await cog.on_message(message)

            # Claude should have been called
            mock_create.assert_called_once()

        # State should have gone back to discussing then stayed there
        assert session.state == "discussing"

    @pytest.mark.asyncio
    async def test_session_timeout(self) -> None:
        """Expired session gets timeout message and is removed."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
        )
        # Set last_active to well past the timeout
        session.last_active = time.monotonic() - 2000
        cog_feature._sessions[5000] = session

        message = _make_thread_message(5000, 111, "hello?")

        await cog.on_message(message)

        send_text = message.channel.send.call_args[0][0]
        assert "timed out" in send_text.lower()
        assert 5000 not in cog_feature._sessions

    @pytest.mark.asyncio
    async def test_messages_in_generating_state_ignored(self) -> None:
        """Messages while code is being generated are ignored."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            state="generating",
        )
        cog_feature._sessions[5000] = session

        message = _make_thread_message(5000, 111, "how's it going?")

        await cog.on_message(message)

        message.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_messages_in_done_state_ignored(self) -> None:
        """Messages after completion are ignored."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            state="done",
        )
        cog_feature._sessions[5000] = session

        message = _make_thread_message(5000, 111, "thanks!")

        await cog.on_message(message)

        message.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_breaker_on_planning_call(self) -> None:
        """Planning call respects circuit breaker."""
        mock_bot = MagicMock()
        cog = cog_feature.FeatureRequestCog(mock_bot)

        session = cog_feature.ThreadSession(
            thread_id=5000, user_id=111, request_type="plugin",
            original_description="add a leaderboard",
            messages=[
                {"role": "user", "content": "Feature request: add a leaderboard"},
            ],
        )
        cog_feature._sessions[5000] = session

        health = ClaudeHealth()
        for _ in range(3):
            health.record_failure()

        message = _make_thread_message(5000, 111, "a points-based one")

        with patch("cog_feature.claude_health", health):
            await cog.on_message(message)

        send_text = message.channel.send.call_args[0][0]
        assert "unavailable" in send_text.lower()

    @pytest.mark.asyncio
    async def test_full_end_to_end_flow(self) -> None:
        """Full flow: trigger -> discuss -> plan_ready -> confirm -> PR."""
        mock_bot = MagicMock()
        mock_bot.user = MagicMock(id=99999)
        cog = cog_feature.FeatureRequestCog(mock_bot)

        # Shared dicts that persist across all 3 on_message calls
        sessions_dict: dict = {}
        last_req_dict: dict = {}

        # Step 1: Initial feature request triggers thread creation
        initial_msg = AsyncMock()
        initial_msg.content = "<@99999> feature request: add a leaderboard"
        initial_msg.channel = MagicMock()
        initial_msg.channel.id = 12345
        initial_msg.reply = AsyncMock()
        initial_msg.author = _make_author(user_id=111, has_role=True)

        mock_thread = AsyncMock()
        mock_thread.id = 5000
        mock_thread.send = AsyncMock()
        initial_msg.create_thread = AsyncMock(return_value=mock_thread)
        initial_msg.mentions = [mock_bot.user]

        # Planning response asks clarifying questions
        planning_resp1 = MagicMock()
        planning_resp1.content = [MagicMock(text="What metrics should the leaderboard track?")]

        with (
            patch.object(cog_feature, "_last_request", last_req_dict),
            patch.object(cog_feature, "_sessions", sessions_dict),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, return_value=planning_resp1,
            ),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(initial_msg)

            diag = (
                f"author.bot={initial_msg.author.bot!r} "
                f"bool(author.bot)={bool(initial_msg.author.bot)} "
                f"bot_user_in_mentions={cog.bot.user in initial_msg.mentions} "
                f"content={initial_msg.content!r} "
                f"reply_calls={initial_msg.reply.call_args_list!r} "
                f"create_thread_called={initial_msg.create_thread.called}"
            )
            assert 5000 in sessions_dict, f"session not in dict. {diag}"
            session = sessions_dict[5000]
            assert session.state == "discussing"

        # Step 2: User answers in thread
        thread_msg1 = _make_thread_message(5000, 111, "Track message count and XP")

        planning_resp2 = MagicMock()
        planning_resp2.content = [MagicMock(
            text="Plan: Create a leaderboard plugin tracking messages and XP.\n---PLAN_READY---"
        )]

        with (
            patch.object(cog_feature, "_sessions", sessions_dict),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, return_value=planning_resp2,
            ),
        ):
            await cog.on_message(thread_msg1)

        assert session.state == "plan_ready"

        # Step 3: User confirms
        confirm_msg = _make_thread_message(5000, 111, "go")

        with (
            patch.object(cog_feature, "_sessions", sessions_dict),
            patch.object(
                cog, "_handle_request",
                new_callable=AsyncMock,
                return_value="https://github.com/user/repo/pull/99",
            ),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(confirm_msg)

            # PR link should be posted
            send_calls = [str(c) for c in confirm_msg.channel.send.call_args_list]
            assert any("pull/99" in c for c in send_calls)
            # Session should be cleaned up
            assert 5000 not in cog_feature._sessions

    @pytest.mark.asyncio
    async def test_initial_plan_ready_on_first_call(self) -> None:
        """If Claude returns PLAN_READY on first message, state goes to plan_ready."""
        mock_bot = MagicMock()
        mock_bot.user = MagicMock(id=99999)
        cog = cog_feature.FeatureRequestCog(mock_bot)

        initial_msg = AsyncMock()
        initial_msg.content = "<@99999> feature request: add a simple ping command"
        initial_msg.channel = MagicMock()
        initial_msg.channel.id = 12345
        initial_msg.reply = AsyncMock()
        initial_msg.mentions = [mock_bot.user]
        initial_msg.author = _make_author(user_id=111, has_role=True)

        mock_thread = AsyncMock()
        mock_thread.id = 6000
        mock_thread.send = AsyncMock()
        initial_msg.create_thread = AsyncMock(return_value=mock_thread)

        # Claude immediately proposes a plan
        planning_resp = MagicMock()
        planning_resp.content = [MagicMock(
            text="Simple enough! Plan: Add !ping command that replies 'Pong!'.\n---PLAN_READY---"
        )]

        sessions_dict: dict = {}
        with (
            patch.object(cog_feature, "_last_request", {}),
            patch.object(cog_feature, "_sessions", sessions_dict),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, return_value=planning_resp,
            ),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(initial_msg)

            diag = (
                f"author.bot={initial_msg.author.bot!r} "
                f"bool(author.bot)={bool(initial_msg.author.bot)} "
                f"bot_user_in_mentions={cog.bot.user in initial_msg.mentions} "
                f"content={initial_msg.content!r} "
                f"reply_calls={initial_msg.reply.call_args_list!r} "
                f"create_thread_called={initial_msg.create_thread.called}"
            )
            assert 6000 in sessions_dict, f"session not in dict. {diag}"
            session = sessions_dict[6000]
            assert session.state == "plan_ready"

            # Thread message should contain the go prompt
            send_text = mock_thread.send.call_args[0][0]
            assert "go" in send_text.lower()
            assert "---PLAN_READY---" not in send_text
