"""Command registry: AST-based command scanning + SQLite persistence.

Scans plugin files for prefix commands (``@commands.command``) and slash
commands (``@app_commands.command``), stores them in the same ``sessions.db``
used by :mod:`session_store`, and provides collision detection for code
generation.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import time
from dataclasses import dataclass

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
DB_PATH: str = os.path.join(PROJECT_DIR, "data", "sessions.db")

# Decorator patterns that define commands
PREFIX_DECORATORS: frozenset[tuple[str, ...]] = frozenset({
    ("commands", "command"),
    ("command",),
})

SLASH_DECORATORS: frozenset[tuple[str, ...]] = frozenset({
    ("app_commands", "command"),
    ("app_command",),
})


@dataclass
class CommandInfo:
    """Metadata about a single registered command."""

    command_name: str
    command_type: str      # "slash" or "prefix"
    plugin_file: str
    description: str
    registered_at: float


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open a connection to the shared database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_commands_table() -> None:
    """Create the commands table if it doesn't exist."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                command_name  TEXT NOT NULL,
                command_type  TEXT NOT NULL,
                plugin_file   TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                registered_at REAL NOT NULL,
                PRIMARY KEY (command_name, command_type)
            )
            """
        )


def rebuild_registry(commands: list[CommandInfo]) -> None:
    """Replace all registry rows with the given commands."""
    with _connect() as conn:
        conn.execute("DELETE FROM commands")
        conn.executemany(
            """
            INSERT INTO commands
                (command_name, command_type, plugin_file, description, registered_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (c.command_name, c.command_type, c.plugin_file,
                 c.description, c.registered_at)
                for c in commands
            ],
        )


def get_all_commands() -> list[CommandInfo]:
    """Return every registered command."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM commands").fetchall()
    return [
        CommandInfo(
            command_name=row["command_name"],
            command_type=row["command_type"],
            plugin_file=row["plugin_file"],
            description=row["description"],
            registered_at=row["registered_at"],
        )
        for row in rows
    ]


def get_taken_names() -> dict[str, list[str]]:
    """Return taken command names grouped by type.

    Example: ``{"slash": ["ping"], "prefix": ["ping", "help"]}``
    """
    commands = get_all_commands()
    result: dict[str, list[str]] = {"slash": [], "prefix": []}
    for cmd in commands:
        result.setdefault(cmd.command_type, []).append(cmd.command_name)
    return result


def check_collisions(new_commands: list[CommandInfo]) -> list[str]:
    """Check *new_commands* against the existing registry.

    Returns a list of human-readable collision descriptions.  An empty list
    means no collisions.
    """
    existing = get_all_commands()
    existing_keys: dict[tuple[str, str], str] = {
        (c.command_name, c.command_type): c.plugin_file for c in existing
    }

    collisions: list[str] = []
    for cmd in new_commands:
        key = (cmd.command_name, cmd.command_type)
        if key in existing_keys:
            collisions.append(
                f"{cmd.command_type} command '{cmd.command_name}' "
                f"already registered by {existing_keys[key]}"
            )
    return collisions


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------

def _decorator_path(node: ast.expr) -> tuple[str, ...] | None:
    """Extract the dotted path of a decorator (ignoring arguments).

    ``@commands.command(name="ping")`` -> ``("commands", "command")``
    ``@command(name="ping")``          -> ``("command",)``
    """
    # Unwrap Call node to get the underlying Name/Attribute
    if isinstance(node, ast.Call):
        node = node.func

    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parts: list[str] = [node.attr]
        inner = node.value
        while isinstance(inner, ast.Attribute):
            parts.append(inner.attr)
            inner = inner.value
        if isinstance(inner, ast.Name):
            parts.append(inner.id)
            return tuple(reversed(parts))
    return None


def _extract_name_kwarg(decorator: ast.expr) -> str | None:
    """Extract ``name=...`` keyword from a decorator call, if present."""
    if not isinstance(decorator, ast.Call):
        return None
    for kw in decorator.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _extract_description_kwarg(decorator: ast.expr) -> str | None:
    """Extract ``description=...`` keyword from a decorator call, if present."""
    if not isinstance(decorator, ast.Call):
        return None
    for kw in decorator.keywords:
        if kw.arg == "description" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _get_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the docstring of a function node, or empty string."""
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        return node.body[0].value.value
    return ""


def scan_file_for_commands(source: str, file_path: str) -> list[CommandInfo]:
    """Parse *source* and return all prefix/slash commands found."""
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return []

    now = time.time()
    commands: list[CommandInfo] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            path = _decorator_path(decorator)
            if path is None:
                continue

            if path in PREFIX_DECORATORS:
                cmd_type = "prefix"
            elif path in SLASH_DECORATORS:
                cmd_type = "slash"
            else:
                continue

            name = _extract_name_kwarg(decorator) or node.name
            description = (
                _extract_description_kwarg(decorator)
                or _get_docstring(node)
            )

            commands.append(CommandInfo(
                command_name=name,
                command_type=cmd_type,
                plugin_file=file_path,
                description=description,
                registered_at=now,
            ))

    return commands


def scan_plugins_directory(plugins_dir: str) -> list[CommandInfo]:
    """Scan all ``.py`` files in *plugins_dir* (skipping ``__init__.py``)."""
    commands: list[CommandInfo] = []
    if not os.path.isdir(plugins_dir):
        return commands

    for filename in sorted(os.listdir(plugins_dir)):
        if not filename.endswith(".py") or filename == "__init__.py":
            continue
        path = os.path.join(plugins_dir, filename)
        with open(path, encoding="utf-8") as f:
            source = f.read()
        rel_path = f"plugins/{filename}"
        commands.extend(scan_file_for_commands(source, rel_path))

    return commands
