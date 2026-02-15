import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

import github_ops


class TestSanitizeBranch:
    def test_lowercase(self) -> None:
        result = github_ops._sanitize_branch("Hello World")
        assert result.startswith("hello-world-")
        # 6-char hex suffix after dash
        assert len(result.split("-")[-1]) == 6

    def test_special_characters(self) -> None:
        result = github_ops._sanitize_branch("add a /ping command!")
        assert result.startswith("add-a--ping-command--")

    def test_truncation(self) -> None:
        long_name = "a" * 100
        result = github_ops._sanitize_branch(long_name)
        # 50 chars slug + 1 dash + 6 hex = 57
        assert len(result) == 57

    def test_preserves_valid_chars(self) -> None:
        result = github_ops._sanitize_branch("my-feature_v2")
        assert result.startswith("my-feature_v2-")

    def test_empty_string(self) -> None:
        result = github_ops._sanitize_branch("")
        # Just the hex suffix
        assert len(result) == 6

    def test_unique_per_call(self) -> None:
        a = github_ops._sanitize_branch("same name")
        b = github_ops._sanitize_branch("same name")
        assert a != b


class TestApplyChanges:
    def test_create_file(self, tmp_path: str) -> None:
        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            github_ops.apply_changes([{
                "path": "new_file.py",
                "action": "create",
                "content": "print('hello')\n",
            }])
            created = os.path.join(str(tmp_path), "new_file.py")
            assert os.path.exists(created)
            with open(created, encoding="utf-8") as f:
                assert f.read() == "print('hello')\n"

    def test_modify_file(self, tmp_path: str) -> None:
        target = os.path.join(str(tmp_path), "existing.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("old content")

        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            github_ops.apply_changes([{
                "path": "existing.py",
                "action": "modify",
                "content": "new content",
            }])
            with open(target, encoding="utf-8") as f:
                assert f.read() == "new content"

    def test_delete_file(self, tmp_path: str) -> None:
        target = os.path.join(str(tmp_path), "doomed.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("bye")

        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            github_ops.apply_changes([{
                "path": "doomed.py",
                "action": "delete",
                "content": "",
            }])
            assert not os.path.exists(target)

    def test_delete_nonexistent_file(self, tmp_path: str) -> None:
        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            # Should not raise
            github_ops.apply_changes([{
                "path": "ghost.py",
                "action": "delete",
                "content": "",
            }])

    def test_create_nested_file(self, tmp_path: str) -> None:
        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            github_ops.apply_changes([{
                "path": "sub/dir/deep.py",
                "action": "create",
                "content": "nested",
            }])
            created = os.path.join(str(tmp_path), "sub", "dir", "deep.py")
            assert os.path.exists(created)


class TestPathTraversal:
    def test_rejects_relative_path_traversal(self, tmp_path: str) -> None:
        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            with pytest.raises(ValueError, match="Path traversal detected"):
                github_ops.apply_changes([{
                    "path": "../../../etc/passwd",
                    "action": "create",
                    "content": "hacked",
                }])

    def test_rejects_absolute_path(self, tmp_path: str) -> None:
        with patch.object(github_ops, "PROJECT_DIR", str(tmp_path)):
            with pytest.raises(ValueError, match="Path traversal detected"):
                github_ops.apply_changes([{
                    "path": "/etc/passwd",
                    "action": "create",
                    "content": "hacked",
                }])

    def test_rejects_sibling_directory_prefix(self, tmp_path: str) -> None:
        """Sibling dir with matching prefix (e.g. turbot-evil) is rejected."""
        project_dir = str(tmp_path / "turbot")
        os.makedirs(project_dir)
        with patch.object(github_ops, "PROJECT_DIR", project_dir):
            with pytest.raises(ValueError, match="Path traversal detected"):
                github_ops.apply_changes([{
                    "path": "../turbot-evil/payload.py",
                    "action": "create",
                    "content": "hacked",
                }])


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_creates_branch_with_sanitized_name(self) -> None:
        with patch.object(github_ops, "_run", new_callable=AsyncMock) as mock_run:
            branch = await github_ops.create_branch("Add ping command")
            assert branch.startswith("feature/add-ping-command-")
            assert mock_run.call_count == 3
            mock_run.assert_any_call(["git", "checkout", "main"])
            mock_run.assert_any_call(["git", "pull", "origin", "main"])
            # Third call is checkout -b with the full branch name
            checkout_call = mock_run.call_args_list[2]
            assert checkout_call[0][0][:3] == ["git", "checkout", "-b"]
            assert checkout_call[0][0][3] == branch


class TestCommitAndPush:
    @pytest.mark.asyncio
    async def test_adds_all_commits_and_pushes(self) -> None:
        with patch.object(github_ops, "_run", new_callable=AsyncMock) as mock_run:
            await github_ops.commit_and_push("feature/test", "test commit")
            assert mock_run.call_count == 3
            mock_run.assert_any_call(["git", "add", "-A"])
            mock_run.assert_any_call(["git", "commit", "-m", "test commit"])
            mock_run.assert_any_call(["git", "push", "-u", "origin", "feature/test"])

    @pytest.mark.asyncio
    async def test_adds_specific_paths_when_provided(self) -> None:
        with patch.object(github_ops, "_run", new_callable=AsyncMock) as mock_run:
            await github_ops.commit_and_push(
                "feature/test", "test commit",
                paths=["plugins/foo.py", "plugins/bar.py"],
            )
            assert mock_run.call_count == 3
            mock_run.assert_any_call(
                ["git", "add", "--", "plugins/foo.py", "plugins/bar.py"]
            )
            mock_run.assert_any_call(["git", "commit", "-m", "test commit"])
            mock_run.assert_any_call(["git", "push", "-u", "origin", "feature/test"])


class TestOpenPr:
    @pytest.mark.asyncio
    async def test_returns_pr_url(self) -> None:
        with patch.object(
            github_ops, "_run", new_callable=AsyncMock,
            return_value="https://github.com/user/repo/pull/42",
        ):
            url = await github_ops.open_pr("feature/test", "title", "body")
            assert url == "https://github.com/user/repo/pull/42"

    @pytest.mark.asyncio
    async def test_raises_on_empty_output(self) -> None:
        with patch.object(
            github_ops, "_run", new_callable=AsyncMock,
            return_value="",
        ):
            with pytest.raises(RuntimeError, match="No PR URL"):
                await github_ops.open_pr("feature/test", "title", "body")

    @pytest.mark.asyncio
    async def test_extracts_last_line(self) -> None:
        with patch.object(
            github_ops, "_run", new_callable=AsyncMock,
            return_value="Creating PR...\nhttps://github.com/user/repo/pull/42",
        ):
            url = await github_ops.open_pr("feature/test", "title", "body")
            assert url == "https://github.com/user/repo/pull/42"


class TestRun:
    @pytest.mark.asyncio
    async def test_returns_stdout(self) -> None:
        result = await github_ops._run(["echo", "hello"])
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_raises_on_failure(self) -> None:
        with pytest.raises(RuntimeError, match="failed"):
            await github_ops._run(["false"])

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self) -> None:
        """A command that exceeds the timeout should be killed."""
        with pytest.raises(RuntimeError, match="timed out"):
            await github_ops._run(["sleep", "10"], timeout=0.1)

    @pytest.mark.asyncio
    async def test_custom_timeout_parameter(self) -> None:
        """Custom timeout should be respected."""
        # This should complete within the generous timeout
        result = await github_ops._run(["echo", "fast"], timeout=5.0)
        assert result == "fast"
