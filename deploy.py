"""Turbot Deploy — handles git pull, pip install, health check, and rollback.

Spawned by bot.py when a merged PR webhook arrives. Runs in a separate systemd
scope (via systemd-run --scope) so it survives the bot service being stopped.
"""

import fcntl
import json
import os
import subprocess
import sys
import time

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE: str = os.path.join(PROJECT_DIR, ".status")
LOG_FILE: str = os.path.join(PROJECT_DIR, "deploy.log")
LOCK_FILE: str = os.path.join(PROJECT_DIR, ".deploy.lock")
HEALTH_TIMEOUT: int = 30
GIT_TIMEOUT: int = 120
PIP_TIMEOUT: int = 300


def log(msg: str) -> None:
    """Append a timestamped message to deploy.log."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def write_status(event: str, **kwargs: str) -> None:
    """Write a status file for the bot to read on startup."""
    data: dict[str, str] = {"event": event, **kwargs}
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_commit() -> str:
    """Get the current HEAD commit hash."""
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR,
        timeout=GIT_TIMEOUT,
    ).decode().strip()


def run_git(args: list[str]) -> None:
    """Run a git command in the project directory."""
    subprocess.check_call(["git", *args], cwd=PROJECT_DIR, timeout=GIT_TIMEOUT)


def install_deps() -> None:
    """Install requirements.txt using the venv pip."""
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=PROJECT_DIR,
        timeout=PIP_TIMEOUT,
    )


def systemctl(action: str) -> None:
    """Run a systemctl --user action on the turbot service."""
    subprocess.check_call(
        ["systemctl", "--user", action, "turbot"],
        timeout=30,
    )


def is_active() -> bool:
    """Check if the turbot service is currently active."""
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "turbot"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


def get_service_pid() -> str:
    """Get the MainPID of the turbot service."""
    result = subprocess.run(
        ["systemctl", "--user", "show", "turbot", "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def rollback(good_commit: str) -> None:
    """Reset to a known good commit and reinstall deps."""
    log(f"Rolling back to {good_commit}")
    run_git(["checkout", "main"])
    run_git(["reset", "--hard", good_commit])
    install_deps()


def main() -> int:
    """Run the deploy flow: stop, pull, install, start, health check."""
    log("Deploy started")
    good_commit = get_commit()

    # Stop the bot service
    log("Stopping bot service...")
    try:
        systemctl("stop")
    except subprocess.CalledProcessError as e:
        log(f"Warning: stop failed (may already be stopped): {e}")

    # Pull latest code and install deps
    try:
        run_git(["pull", "origin", "main"])
        install_deps()
    except Exception as e:
        log(f"Deploy failed during pull/install: {e}")
        rollback(good_commit)
        write_status(
            "deploy_pull_failed",
            error=str(e),
            good_commit=good_commit,
        )
        systemctl("start")
        return 1

    new_commit = get_commit()
    log(f"Pulled {new_commit[:8]}. Starting bot with health check...")

    # Write deploy status and start the bot
    write_status("deploy_success", commit=new_commit)
    systemctl("start")

    # Health check: verify bot stays alive for HEALTH_TIMEOUT seconds
    time.sleep(2)  # Let the process start
    start_pid = get_service_pid()

    time.sleep(HEALTH_TIMEOUT)

    current_pid = get_service_pid()
    if start_pid != current_pid or not is_active():
        log(f"Bot crashed within {HEALTH_TIMEOUT}s. Rolling back.")
        try:
            systemctl("stop")
        except subprocess.CalledProcessError:
            pass
        rollback(good_commit)
        write_status(
            "rollback",
            bad_commit=new_commit,
            good_commit=good_commit,
        )
        systemctl("start")
        return 1

    log(f"Deploy successful! Bot healthy after {HEALTH_TIMEOUT}s")
    return 0


if __name__ == "__main__":
    # Acquire exclusive lock to prevent concurrent deploys
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another deploy is already running. Skipping.")
        os.close(lock_fd)
        sys.exit(0)

    try:
        sys.exit(main())
    finally:
        os.close(lock_fd)
