import json
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, call, patch

import pytest

import supervisor


class TestWriteStatus:
    def test_writes_json_file(self, tmp_path: str) -> None:
        path = os.path.join(str(tmp_path), ".status")
        with patch.object(supervisor, "STATUS_FILE", path):
            supervisor.write_status("deploy_success", commit="abc123")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            assert data == {"event": "deploy_success", "commit": "abc123"}

    def test_overwrites_existing(self, tmp_path: str) -> None:
        path = os.path.join(str(tmp_path), ".status")
        with patch.object(supervisor, "STATUS_FILE", path):
            supervisor.write_status("restart")
            supervisor.write_status("rollback", bad_commit="bad", good_commit="good")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            assert data["event"] == "rollback"


class TestLog:
    def test_writes_to_file(self, tmp_path: str) -> None:
        path = os.path.join(str(tmp_path), "supervisor.log")
        with patch.object(supervisor, "LOG_FILE", path):
            supervisor.log("test message")
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "test message" in content

    def test_appends_to_file(self, tmp_path: str) -> None:
        path = os.path.join(str(tmp_path), "supervisor.log")
        with patch.object(supervisor, "LOG_FILE", path):
            supervisor.log("first")
            supervisor.log("second")
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "first" in content
            assert "second" in content


class TestGetCurrentCommit:
    def test_returns_commit_hash(self) -> None:
        with patch.object(
            subprocess, "check_output",
            return_value=b"abc123def456\n",
        ) as mock:
            assert supervisor.get_current_commit() == "abc123def456"
            mock.assert_called_once_with(
                ["git", "rev-parse", "HEAD"],
                cwd=supervisor.PROJECT_DIR,
                timeout=supervisor.GIT_TIMEOUT,
            )


class TestRunGit:
    def test_calls_git_with_args(self) -> None:
        with patch.object(subprocess, "check_call") as mock:
            supervisor.run_git(["pull", "origin", "main"])
            mock.assert_called_once_with(
                ["git", "pull", "origin", "main"],
                cwd=supervisor.PROJECT_DIR,
                timeout=supervisor.GIT_TIMEOUT,
            )


class TestRollback:
    def test_checkouts_resets_and_installs(self) -> None:
        with (
            patch.object(supervisor, "log"),
            patch.object(supervisor, "run_git") as mock_git,
            patch.object(supervisor, "install_deps") as mock_deps,
        ):
            supervisor.rollback("abc123")
            mock_git.assert_any_call(["checkout", "abc123"])
            mock_git.assert_any_call(["reset", "--hard", "abc123"])
            mock_deps.assert_called_once()


class TestHandleSignal:
    def test_sets_shutting_down(self) -> None:
        supervisor.shutting_down = False
        with patch.object(supervisor, "log"):
            supervisor.handle_signal(15, None)
        assert supervisor.shutting_down is True
        # Reset
        supervisor.shutting_down = False


class TestMainLoop:
    def test_deploy_flow_success(self, tmp_path: str) -> None:
        """Simulate: bot exits with .deploy signal, pull succeeds, health check passes."""
        deploy_path = os.path.join(str(tmp_path), ".deploy")
        status_path = os.path.join(str(tmp_path), ".status")

        # Create the deploy signal
        with open(deploy_path, "w", encoding="utf-8") as f:
            f.write("deploy")

        call_count = 0

        def mock_popen(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            proc = MagicMock()
            if call_count == 1:
                # First bot run — exits normally (triggers deploy check)
                proc.wait.return_value = None
                proc.returncode = 0
            elif call_count == 2:
                # Health check bot — survives timeout
                proc.wait.side_effect = subprocess.TimeoutExpired(cmd="bot", timeout=30)
                # After timeout, the main wait call returns normally
                proc.wait.side_effect = [
                    subprocess.TimeoutExpired(cmd="bot", timeout=30),
                    None,
                ]
                proc.returncode = 0
            else:
                # Third call — we shut down
                proc.wait.return_value = None
                proc.returncode = 0
            return proc

        with (
            patch.object(supervisor, "DEPLOY_SIGNAL", deploy_path),
            patch.object(supervisor, "STATUS_FILE", status_path),
            patch.object(supervisor, "log"),
            patch.object(supervisor, "get_current_commit", return_value="abc123"),
            patch.object(supervisor, "run_git"),
            patch.object(supervisor, "install_deps"),
            patch.object(subprocess, "Popen", side_effect=mock_popen),
        ):
            # After 3 iterations, stop
            original_shutting_down = supervisor.shutting_down

            iteration = [0]
            original_log = supervisor.log

            def counting_log(msg: str) -> None:
                iteration[0] += 1
                if iteration[0] > 8:
                    supervisor.shutting_down = True

            with patch.object(supervisor, "log", side_effect=counting_log):
                supervisor.main()

            supervisor.shutting_down = False

    def test_deploy_rollback_on_crash_within_health_timeout(self, tmp_path: str) -> None:
        """Bot crashes within health timeout after deploy — should rollback."""
        deploy_path = os.path.join(str(tmp_path), ".deploy")
        status_path = os.path.join(str(tmp_path), ".status")

        # Create the deploy signal
        with open(deploy_path, "w", encoding="utf-8") as f:
            f.write("deploy")

        call_count = 0

        def mock_popen(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            proc = MagicMock()
            if call_count == 1:
                # First bot run — exits normally (triggers deploy check)
                proc.wait.return_value = None
                proc.returncode = 0
            elif call_count == 2:
                # Health check bot — crashes immediately (within timeout)
                proc.wait.return_value = None
                proc.returncode = 1
            else:
                # After rollback, shut down
                proc.wait.return_value = None
                proc.returncode = 0
                supervisor.shutting_down = True
            return proc

        with (
            patch.object(supervisor, "DEPLOY_SIGNAL", deploy_path),
            patch.object(supervisor, "STATUS_FILE", status_path),
            patch.object(supervisor, "log"),
            patch.object(supervisor, "get_current_commit", return_value="abc123"),
            patch.object(supervisor, "run_git"),
            patch.object(supervisor, "install_deps"),
            patch.object(supervisor, "rollback") as mock_rollback,
            patch.object(subprocess, "Popen", side_effect=mock_popen),
            patch("time.sleep"),
        ):
            supervisor.main()
            supervisor.shutting_down = False

        # Should have rolled back to the known good commit
        mock_rollback.assert_called_once_with("abc123")

        # Status file should reflect the rollback
        assert os.path.exists(status_path)
        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["event"] == "rollback"
        assert data["good_commit"] == "abc123"

    def test_normal_restart_on_crash(self, tmp_path: str) -> None:
        """Bot crashes without deploy signal — should restart after delay."""
        status_path = os.path.join(str(tmp_path), ".status")
        deploy_path = os.path.join(str(tmp_path), ".deploy")

        calls = [0]

        def mock_popen(*args: object, **kwargs: object) -> MagicMock:
            calls[0] += 1
            proc = MagicMock()
            proc.wait.return_value = None
            proc.returncode = 1
            if calls[0] >= 2:
                supervisor.shutting_down = True
            return proc

        with (
            patch.object(supervisor, "DEPLOY_SIGNAL", deploy_path),
            patch.object(supervisor, "STATUS_FILE", status_path),
            patch.object(supervisor, "log"),
            patch.object(supervisor, "get_current_commit", return_value="abc123"),
            patch("time.sleep"),
            patch.object(subprocess, "Popen", side_effect=mock_popen),
        ):
            supervisor.main()
            supervisor.shutting_down = False

        # Should have written a restart status on second boot
        assert os.path.exists(status_path)
        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["event"] == "restart"
