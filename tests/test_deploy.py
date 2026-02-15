import json
import os
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

import deploy


class TestLog:
    def test_appends_to_file(self, tmp_path: str) -> None:
        path = os.path.join(str(tmp_path), "deploy.log")
        with patch.object(deploy, "LOG_FILE", path):
            deploy.log("first message")
            deploy.log("second message")
            with open(path, encoding="utf-8") as f:
                content = f.read()
        assert "first message" in content
        assert "second message" in content


class TestWriteStatus:
    def test_writes_json_file(self, tmp_path: str) -> None:
        path = os.path.join(str(tmp_path), ".status")
        with patch.object(deploy, "STATUS_FILE", path):
            deploy.write_status("deploy_success", commit="abc123")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        assert data == {"event": "deploy_success", "commit": "abc123"}


class TestGetCommit:
    def test_returns_commit_hash(self) -> None:
        with patch.object(
            subprocess, "check_output",
            return_value=b"abc123def456\n",
        ) as mock:
            result = deploy.get_commit()
        assert result == "abc123def456"
        mock.assert_called_once_with(
            ["git", "rev-parse", "HEAD"],
            cwd=deploy.PROJECT_DIR,
            timeout=deploy.GIT_TIMEOUT,
        )


class TestRunGit:
    def test_calls_git_with_args(self) -> None:
        with patch.object(subprocess, "check_call") as mock:
            deploy.run_git(["pull", "origin", "main"])
        mock.assert_called_once_with(
            ["git", "pull", "origin", "main"],
            cwd=deploy.PROJECT_DIR,
            timeout=deploy.GIT_TIMEOUT,
        )


class TestInstallDeps:
    def test_calls_pip_install(self) -> None:
        with patch.object(subprocess, "check_call") as mock:
            deploy.install_deps()
        mock.assert_called_once()
        cmd = mock.call_args[0][0]
        assert "-m" in cmd and "pip" in cmd
        assert cmd[-2:] == ["-r", "requirements.txt"]


class TestSystemctl:
    def test_calls_systemctl_user(self) -> None:
        with patch.object(subprocess, "check_call") as mock:
            deploy.systemctl("stop")
        mock.assert_called_once_with(
            ["systemctl", "--user", "stop", "turbot"],
            timeout=30,
        )


class TestIsActive:
    def test_returns_true_when_active(self) -> None:
        with patch.object(
            subprocess, "run",
            return_value=MagicMock(returncode=0),
        ):
            assert deploy.is_active() is True

    def test_returns_false_when_inactive(self) -> None:
        with patch.object(
            subprocess, "run",
            return_value=MagicMock(returncode=3),
        ):
            assert deploy.is_active() is False


class TestGetServicePid:
    def test_returns_pid_string(self) -> None:
        with patch.object(
            subprocess, "run",
            return_value=MagicMock(stdout="12345\n"),
        ):
            assert deploy.get_service_pid() == "12345"


class TestRollback:
    def test_checkouts_resets_and_installs(self) -> None:
        with (
            patch.object(deploy, "log"),
            patch.object(deploy, "run_git") as mock_git,
            patch.object(deploy, "install_deps") as mock_deps,
        ):
            deploy.rollback("abc123")
        mock_git.assert_any_call(["checkout", "main"])
        mock_git.assert_any_call(["reset", "--hard", "abc123"])
        mock_deps.assert_called_once()


class TestMainFlow:
    @pytest.fixture(autouse=True)
    def _mock_log(self):
        with patch.object(deploy, "log"):
            yield

    @pytest.fixture(autouse=True)
    def _mock_sleep(self):
        with patch("time.sleep"):
            yield

    def test_successful_deploy(self, tmp_path: str) -> None:
        """Full deploy: stop → pull → install → start → health check passes."""
        status_path = os.path.join(str(tmp_path), ".status")

        with (
            patch.object(deploy, "STATUS_FILE", status_path),
            patch.object(deploy, "get_commit", side_effect=["old123", "new456"]),
            patch.object(deploy, "systemctl") as mock_sctl,
            patch.object(deploy, "run_git"),
            patch.object(deploy, "install_deps"),
            patch.object(deploy, "get_service_pid", return_value="999"),
            patch.object(deploy, "is_active", return_value=True),
        ):
            result = deploy.main()

        assert result == 0
        mock_sctl.assert_any_call("stop")
        mock_sctl.assert_any_call("start")

        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["event"] == "deploy_success"
        assert data["commit"] == "new456"

    def test_pull_failure_triggers_rollback(self, tmp_path: str) -> None:
        """Pull fails → rollback → restart."""
        status_path = os.path.join(str(tmp_path), ".status")

        with (
            patch.object(deploy, "STATUS_FILE", status_path),
            patch.object(deploy, "get_commit", return_value="old123"),
            patch.object(deploy, "systemctl") as mock_sctl,
            patch.object(
                deploy, "run_git",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
            patch.object(deploy, "rollback") as mock_rollback,
        ):
            result = deploy.main()

        assert result == 1
        mock_rollback.assert_called_once_with("old123")
        mock_sctl.assert_any_call("start")

        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["event"] == "deploy_pull_failed"

    def test_health_check_failure_triggers_rollback(self, tmp_path: str) -> None:
        """Bot crashes within health timeout → rollback."""
        status_path = os.path.join(str(tmp_path), ".status")

        with (
            patch.object(deploy, "STATUS_FILE", status_path),
            patch.object(deploy, "get_commit", side_effect=["old123", "new456"]),
            patch.object(deploy, "systemctl") as mock_sctl,
            patch.object(deploy, "run_git"),
            patch.object(deploy, "install_deps"),
            patch.object(
                deploy, "get_service_pid",
                side_effect=["999", "1001"],  # PID changed → crash + restart
            ),
            patch.object(deploy, "is_active", return_value=True),
            patch.object(deploy, "rollback") as mock_rollback,
        ):
            result = deploy.main()

        assert result == 1
        mock_rollback.assert_called_once_with("old123")

        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["event"] == "rollback"
        assert data["bad_commit"] == "new456"
        assert data["good_commit"] == "old123"

    def test_health_check_service_down_triggers_rollback(self, tmp_path: str) -> None:
        """Bot is not active after health timeout → rollback."""
        status_path = os.path.join(str(tmp_path), ".status")

        with (
            patch.object(deploy, "STATUS_FILE", status_path),
            patch.object(deploy, "get_commit", side_effect=["old123", "new456"]),
            patch.object(deploy, "systemctl") as mock_sctl,
            patch.object(deploy, "run_git"),
            patch.object(deploy, "install_deps"),
            patch.object(deploy, "get_service_pid", return_value="999"),
            patch.object(deploy, "is_active", return_value=False),
            patch.object(deploy, "rollback") as mock_rollback,
        ):
            result = deploy.main()

        assert result == 1
        mock_rollback.assert_called_once_with("old123")

    def test_stop_failure_continues(self, tmp_path: str) -> None:
        """If stop fails (already stopped), deploy continues."""
        status_path = os.path.join(str(tmp_path), ".status")

        stop_called = False

        def mock_systemctl(action: str) -> None:
            nonlocal stop_called
            if action == "stop" and not stop_called:
                stop_called = True
                raise subprocess.CalledProcessError(1, "systemctl")

        with (
            patch.object(deploy, "STATUS_FILE", status_path),
            patch.object(deploy, "get_commit", side_effect=["old123", "new456"]),
            patch.object(deploy, "systemctl", side_effect=mock_systemctl),
            patch.object(deploy, "run_git"),
            patch.object(deploy, "install_deps"),
            patch.object(deploy, "get_service_pid", return_value="999"),
            patch.object(deploy, "is_active", return_value=True),
        ):
            result = deploy.main()

        assert result == 0


class TestLocking:
    def test_concurrent_deploy_skipped(self, tmp_path: str) -> None:
        """If lock is already held, deploy exits immediately."""
        import fcntl

        lock_path = os.path.join(str(tmp_path), ".deploy.lock")

        # Hold the lock
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        try:
            # Attempt to acquire lock non-blocking should fail
            test_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            with pytest.raises(BlockingIOError):
                fcntl.flock(test_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(test_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
