import asyncio
import os
import re
import secrets

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
SUBPROCESS_TIMEOUT: float = 60.0


async def _run(
    cmd: list[str],
    cwd: str | None = None,
    timeout: float = SUBPROCESS_TIMEOUT,
) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or PROJECT_DIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"Command {cmd} timed out after {timeout}s"
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command {cmd} failed (rc={proc.returncode}): {stderr.decode()}"
        )
    return stdout.decode().strip()


def _sanitize_branch(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower())[:50]
    suffix = secrets.token_hex(3)
    return f"{slug}-{suffix}" if slug else suffix


async def create_branch(name: str) -> str:
    branch = f"feature/{_sanitize_branch(name)}"
    await _run(["git", "checkout", "main"])
    await _run(["git", "pull", "origin", "main"])
    await _run(["git", "checkout", "-b", branch])
    return branch


def apply_changes(changes: list[dict[str, str]]) -> None:
    for change in changes:
        path = os.path.normpath(os.path.join(PROJECT_DIR, change["path"]))
        if not path.startswith(PROJECT_DIR + os.sep) and path != PROJECT_DIR:
            raise ValueError(f"Path traversal detected: {change['path']}")
        action = change["action"]
        if action in ("create", "modify"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(change["content"])
        elif action == "delete":
            if os.path.exists(path):
                os.remove(path)


async def commit_and_push(
    branch: str, message: str, paths: list[str] | None = None,
) -> None:
    if paths:
        await _run(["git", "add", "--"] + paths)
    else:
        await _run(["git", "add", "-A"])
    await _run(["git", "commit", "-m", message])
    await _run(["git", "push", "-u", "origin", branch])


async def open_pr(branch: str, title: str, body: str) -> str:
    result = await _run([
        "gh", "pr", "create",
        "--base", "main",
        "--head", branch,
        "--title", title,
        "--body", body,
    ])
    # gh pr create prints the PR URL as the last line
    lines = result.strip().splitlines()
    if not lines:
        raise RuntimeError("No PR URL returned by 'gh pr create'")
    return lines[-1]


async def get_current_commit() -> str:
    return await _run(["git", "rev-parse", "HEAD"])


async def checkout_and_pull(ref: str) -> None:
    await _run(["git", "checkout", ref])
    await _run(["git", "pull", "origin", ref])
