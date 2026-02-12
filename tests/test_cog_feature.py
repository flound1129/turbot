"""Tests for the feature request cog (dual-path: plugin vs core)."""

import json
import re
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import anthropic
import pytest

from api_health import ClaudeHealth
import cog_feature


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
        message.author.bot = is_bot
        message.content = content
        message.channel.id = 12345
        message.reply = AsyncMock()

        bot_user = MagicMock()
        bot_user.id = 99999

        if mentioned:
            message.mentions = [bot_user]
        else:
            message.mentions = []

        if has_role:
            role = MagicMock()
            role.name = role_name

            import discord
            message.author.__class__ = discord.Member
            message.author.roles = [role]
        else:
            import discord
            message.author.__class__ = discord.Member
            message.author.roles = []

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

        with patch("cog_feature.isinstance", side_effect=lambda obj, cls: True):
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

        with patch("cog_feature.isinstance", side_effect=lambda obj, cls: True):
            await cog.on_message(message)

        calls = [str(c) for c in message.reply.call_args_list]
        assert any("describe the feature" in c.lower() for c in calls)

    @pytest.mark.asyncio
    async def test_successful_plugin_request(self) -> None:
        message, bot_user = self._make_message(
            "<@99999> feature request: add a ping command",
            has_role=True,
            role_name="BotAdmin",
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({
            "changes": [{
                "path": "plugins/ping.py",
                "action": "create",
                "content": "import json\n",
            }],
            "summary": "Added ping plugin",
            "title": "Add ping plugin",
        }))]

        with (
            patch("cog_feature.isinstance", side_effect=lambda obj, cls: True),
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_plugin_context", return_value={"plugin_api.py": "# api"}),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch("github_ops.create_branch", new_callable=AsyncMock, return_value="feature/add-a-ping-command"),
            patch("github_ops.apply_changes"),
            patch("github_ops.commit_and_push", new_callable=AsyncMock),
            patch("github_ops.open_pr", new_callable=AsyncMock, return_value="https://github.com/user/repo/pull/1"),
            patch("github_ops._run", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

        reply_calls = [str(c) for c in message.reply.call_args_list]
        assert any("Turbotastic" in c for c in reply_calls)
        assert any("pull/1" in c for c in reply_calls)

    @pytest.mark.asyncio
    async def test_successful_core_request(self) -> None:
        message, bot_user = self._make_message(
            "<@99999> bot improvement: fix a bug in bot.py",
            has_role=True,
            role_name="BotAdmin",
        )
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        claude_response = MagicMock()
        claude_response.content = [MagicMock(text=json.dumps({
            "changes": [{
                "path": "bot.py",
                "action": "modify",
                "content": "# fixed bot",
            }],
            "summary": "Fixed bug in bot.py",
            "title": "Fix bot bug",
        }))]

        with (
            patch("cog_feature.isinstance", side_effect=lambda obj, cls: True),
            patch.object(cog.client.messages, "create", new_callable=AsyncMock, return_value=claude_response),
            patch.object(cog_feature, "_read_project_files", return_value={"bot.py": "# bot"}),
            patch.object(cog_feature, "_log", new_callable=AsyncMock) as mock_log,
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch("github_ops.create_branch", new_callable=AsyncMock, return_value="feature/fix-a-bug"),
            patch("github_ops.apply_changes"),
            patch("github_ops.commit_and_push", new_callable=AsyncMock),
            patch("github_ops.open_pr", new_callable=AsyncMock, return_value="https://github.com/user/repo/pull/2"),
            patch("github_ops._run", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

        reply_calls = [str(c) for c in message.reply.call_args_list]
        assert any("Turbotastic" in c for c in reply_calls)
        # Core change should trigger admin warning
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("CORE CHANGE" in c for c in log_calls)


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

            # Branch was created but apply_changes should NOT have been called
            # because the violation check happens before apply
            # Actually, violations are checked after Claude responds but before PR creation
            # In our code, branch is created first then violations are checked
            # Let's verify the ValueError is raised


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


class TestCogCircuitBreaker:
    """Tests for circuit breaker integration in FeatureRequestCog."""

    def _make_message(
        self,
        content: str = "<@99999> feature request: add a joke command",
        *,
        has_role: bool = True,
    ) -> tuple[MagicMock, MagicMock]:
        message = AsyncMock()
        message.author.bot = False
        message.content = content
        message.channel.id = 12345
        message.reply = AsyncMock()

        bot_user = MagicMock()
        bot_user.id = 99999
        message.mentions = [bot_user]

        if has_role:
            role = MagicMock()
            role.name = "BotAdmin"
            import discord
            message.author.__class__ = discord.Member
            message.author.roles = [role]
        else:
            import discord
            message.author.__class__ = discord.Member
            message.author.roles = []

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
            patch("cog_feature.claude_health", health),
            patch("cog_feature.isinstance", side_effect=lambda obj, cls: True),
            patch.object(cog.client.messages, "create") as mock_create,
        ):
            await cog.on_message(message)

        mock_create.assert_not_called()
        reply_calls = [str(c) for c in message.reply.call_args_list]
        assert any("unavailable" in c.lower() for c in reply_calls)

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
        """Connectivity error in on_message triggers circuit breaker."""
        message, bot_user = self._make_message()
        mock_bot = MagicMock()
        mock_bot.user = bot_user
        cog = cog_feature.FeatureRequestCog(mock_bot)

        health = ClaudeHealth()

        with (
            patch("cog_feature.claude_health", health),
            patch("cog_feature.isinstance", side_effect=lambda obj, cls: True),
            patch.object(
                cog.client.messages, "create",
                new_callable=AsyncMock, side_effect=anthropic.APITimeoutError(request=None),
            ),
            patch.object(cog_feature, "_read_plugin_context", return_value={}),
            patch.object(cog_feature, "_load_security_policy", return_value="# policy"),
            patch.object(cog_feature, "_log", new_callable=AsyncMock),
        ):
            await cog.on_message(message)

        assert health._failures == 1
        reply_calls = [str(c) for c in message.reply.call_args_list]
        assert any("unavailable" in c.lower() for c in reply_calls)
