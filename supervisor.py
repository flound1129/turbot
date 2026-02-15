"""Turbot Supervisor — manages the bot lifecycle, deploys, and rollbacks."""

import json
import os
import signal
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
DEPLOY_SIGNAL: str = os.path.join(PROJECT_DIR, ".deploy")
STATUS_FILE: str = os.path.join(PROJECT_DIR, ".status")
HEALTH_TIMEOUT: int = 30  # seconds — if bot crashes faster than this, it's a bad deploy
GIT_TIMEOUT: int = 120  # seconds for git operations
PIP_TIMEOUT: int = 300  # seconds for pip install
LOG_FILE: str = os.path.join(PROJECT_DIR, "supervisor.log")
LOG_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

shutting_down: bool = False


def _rotate_log() -> None:
    """Rotate the log file if it exceeds LOG_MAX_BYTES."""
    if not os.path.exists(LOG_FILE):
        return
    try:
        if os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
            rotated = LOG_FILE + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.rename(LOG_FILE, rotated)
    except OSError:
        pass


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    _rotate_log()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_status(event: str, **kwargs: str) -> None:
    """Write a status file for the bot to read on startup."""
    data: dict[str, str] = {"event": event, **kwargs}
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_current_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR,
        timeout=GIT_TIMEOUT,
    ).decode().strip()


def run_git(args: list[str]) -> None:
    subprocess.check_call(["git", *args], cwd=PROJECT_DIR, timeout=GIT_TIMEOUT)


def install_deps() -> None:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=PROJECT_DIR,
        timeout=PIP_TIMEOUT,
    )


def rollback(commit: str) -> None:
    log(f"Rolling back to {commit}")
    run_git(["checkout", "main"])
    run_git(["reset", "--hard", commit])
    install_deps()


def handle_signal(signum: int, _frame: types.FrameType | None) -> None:
    global shutting_down
    shutting_down = True
    log(f"Received signal {signum} — shutting down.")


def graceful_stop(proc: subprocess.Popen, timeout: int = 10) -> None:
    """Stop the bot gracefully via webhook, falling back to terminate/kill."""
    # Step 1: Try HTTP shutdown request
    if WEBHOOK_SECRET:
        url = f"http://localhost:{WEBHOOK_PORT}/shutdown"
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            req.add_header("X-Shutdown-Secret", WEBHOOK_SECRET)
            urllib.request.urlopen(req, timeout=5)
            log("Sent shutdown request to bot.")
        except Exception as e:
            log(f"Shutdown request failed: {e}")

    # Step 2: Wait for process to exit
    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.5)

    if proc.poll() is not None:
        log(f"Bot exited cleanly (code {proc.returncode}).")
        return

    # Step 3: SIGTERM
    log("Bot didn't exit in time — sending SIGTERM.")
    proc.terminate()

    term_deadline = time.time() + 3
    while proc.poll() is None and time.time() < term_deadline:
        time.sleep(0.5)

    if proc.poll() is not None:
        log(f"Bot exited after SIGTERM (code {proc.returncode}).")
        return

    # Step 4: SIGKILL
    log("Bot didn't respond to SIGTERM — sending SIGKILL.")
    proc.kill()
    proc.wait()
    log(f"Bot killed (code {proc.returncode}).")


def _wait_for_exit(proc: subprocess.Popen) -> None:
    """Wait for proc to exit, calling graceful_stop if shutdown is requested."""
    while proc.poll() is None:
        if shutting_down:
            graceful_stop(proc)
            return
        time.sleep(1)


def main() -> None:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_known_good = get_current_commit()
    log(f"Turbot supervisor started. Current commit: {last_known_good}")

    first_boot = True

    while not shutting_down:
        log("Starting bot process...")
        start_time = time.time()

        # On first boot with no status file, don't write one (bot says "online")
        # On subsequent restarts without deploy, write a restart status
        if not first_boot and not os.path.exists(STATUS_FILE):
            write_status("restart")
        first_boot = False

        proc = subprocess.Popen(
            [sys.executable, os.path.join(PROJECT_DIR, "bot.py")],
            cwd=PROJECT_DIR,
        )

        try:
            _wait_for_exit(proc)
        except Exception:
            graceful_stop(proc)
            break

        elapsed = time.time() - start_time
        exit_code = proc.returncode
        log(f"Bot exited with code {exit_code} after {elapsed:.1f}s")

        if shutting_down:
            break

        # --- Deploy flow ---
        if os.path.exists(DEPLOY_SIGNAL):
            log("Deploy signal detected.")
            last_known_good = get_current_commit()

            try:
                os.remove(DEPLOY_SIGNAL)
                run_git(["pull", "origin", "main"])
                install_deps()
                log("Deploy: pulled latest and installed deps.")
            except Exception as e:
                log(f"Deploy failed during pull/install: {e}")
                rollback(last_known_good)
                write_status(
                    "deploy_pull_failed",
                    error=str(e),
                    good_commit=last_known_good,
                )
                continue

            # Start the bot and check if it survives the health timeout
            new_commit = get_current_commit()
            log(f"Starting bot (health check: {HEALTH_TIMEOUT}s)...")
            write_status("deploy_success", commit=new_commit)

            health_start = time.time()
            health_proc = subprocess.Popen(
                [sys.executable, os.path.join(PROJECT_DIR, "bot.py")],
                cwd=PROJECT_DIR,
            )

            try:
                health_proc.wait(timeout=HEALTH_TIMEOUT)
                # If we get here, the bot exited within the timeout — bad deploy
                log(f"Bot crashed within {HEALTH_TIMEOUT}s — rolling back.")
                rollback(last_known_good)
                write_status(
                    "rollback",
                    bad_commit=new_commit,
                    good_commit=last_known_good,
                )
            except subprocess.TimeoutExpired:
                # Bot survived the health check — it's good
                log("Bot passed health check. Deploy successful!")
                last_known_good = get_current_commit()
                # Continue monitoring — the bot is still running
                try:
                    _wait_for_exit(health_proc)
                except Exception:
                    graceful_stop(health_proc)
                elapsed = time.time() - health_start
                log(f"Bot exited after {elapsed:.1f}s (post-deploy)")
                # Loop continues and will restart normally

            continue

        # --- Normal crash — just restart ---
        log("No deploy signal — restarting bot in 5s...")
        time.sleep(5)

    log("Supervisor exiting.")


if __name__ == "__main__":
    main()
