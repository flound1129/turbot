"""Tests for command_registry: AST scanning + SQLite registry."""

import os
import sqlite3
import time
from unittest.mock import patch

import pytest

import command_registry


# ---------------------------------------------------------------------------
# AST scanning tests
# ---------------------------------------------------------------------------

class TestScanFileForCommands:
    def test_detects_prefix_command_with_name(self) -> None:
        source = '''
from discord.ext import commands
class P:
    @commands.command(name="ping")
    async def ping_cmd(self, ctx):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/ping.py")
        assert len(cmds) == 1
        assert cmds[0].command_name == "ping"
        assert cmds[0].command_type == "prefix"
        assert cmds[0].plugin_file == "plugins/ping.py"

    def test_detects_prefix_command_without_name(self) -> None:
        source = '''
from discord.ext import commands
class P:
    @commands.command()
    async def ping(self, ctx):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/ping.py")
        assert len(cmds) == 1
        assert cmds[0].command_name == "ping"
        assert cmds[0].command_type == "prefix"

    def test_detects_slash_command_with_name(self) -> None:
        source = '''
from discord import app_commands
class P:
    @app_commands.command(name="roll")
    async def roll_cmd(self, interaction):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/roll.py")
        assert len(cmds) == 1
        assert cmds[0].command_name == "roll"
        assert cmds[0].command_type == "slash"

    def test_detects_slash_command_without_name(self) -> None:
        source = '''
from discord import app_commands
class P:
    @app_commands.command()
    async def roll(self, interaction):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/roll.py")
        assert len(cmds) == 1
        assert cmds[0].command_name == "roll"
        assert cmds[0].command_type == "slash"

    def test_detects_aliased_decorators(self) -> None:
        source = '''
from plugin_api import command, app_command
class P:
    @command(name="ping")
    async def ping(self, ctx):
        pass

    @app_command(name="roll")
    async def roll(self, interaction):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/mixed.py")
        assert len(cmds) == 2
        names = {(c.command_name, c.command_type) for c in cmds}
        assert ("ping", "prefix") in names
        assert ("roll", "slash") in names

    def test_extracts_description_from_decorator(self) -> None:
        source = '''
from discord import app_commands
class P:
    @app_commands.command(name="roll", description="Roll some dice")
    async def roll(self, interaction):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/roll.py")
        assert cmds[0].description == "Roll some dice"

    def test_extracts_description_from_docstring(self) -> None:
        source = '''
from discord.ext import commands
class P:
    @commands.command(name="ping")
    async def ping(self, ctx):
        """Reply with pong."""
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/ping.py")
        assert cmds[0].description == "Reply with pong."

    def test_decorator_description_preferred_over_docstring(self) -> None:
        source = '''
from discord import app_commands
class P:
    @app_commands.command(name="roll", description="From decorator")
    async def roll(self, interaction):
        """From docstring."""
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/roll.py")
        assert cmds[0].description == "From decorator"

    def test_multiple_commands_in_one_file(self) -> None:
        source = '''
from discord.ext import commands
from discord import app_commands
class P:
    @commands.command(name="ping")
    async def ping(self, ctx):
        pass

    @app_commands.command(name="roll")
    async def roll(self, interaction):
        pass

    @commands.command(name="help2")
    async def help2(self, ctx):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/multi.py")
        assert len(cmds) == 3

    def test_no_commands_returns_empty(self) -> None:
        source = '''
class NotAPlugin:
    def regular_method(self):
        pass
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/empty.py")
        assert cmds == []

    def test_syntax_error_returns_empty(self) -> None:
        source = "def bad(:\n    pass"
        cmds = command_registry.scan_file_for_commands(source, "plugins/bad.py")
        assert cmds == []

    def test_non_command_decorators_ignored(self) -> None:
        source = '''
from discord.ext import commands
class P:
    @commands.Cog.listener()
    async def on_message(self, message):
        pass

    @property
    def name(self):
        return "test"
'''
        cmds = command_registry.scan_file_for_commands(source, "plugins/listeners.py")
        assert cmds == []


# ---------------------------------------------------------------------------
# SQLite registry tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def _use_temp_db(tmp_path):
    """Point command_registry at a temporary database."""
    db_path = str(tmp_path / "test.db")
    with patch.object(command_registry, "DB_PATH", db_path):
        command_registry.init_commands_table()
        yield db_path


class TestSQLiteRegistry:
    def test_creates_table(self, _use_temp_db) -> None:
        conn = sqlite3.connect(_use_temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='commands'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_init_idempotent(self, _use_temp_db) -> None:
        # Calling init again should not raise
        command_registry.init_commands_table()

    def test_rebuild_inserts_and_clears_previous(self, _use_temp_db) -> None:
        now = time.time()
        commands1 = [
            command_registry.CommandInfo("ping", "prefix", "plugins/ping.py", "Pong", now),
        ]
        command_registry.rebuild_registry(commands1)
        assert len(command_registry.get_all_commands()) == 1

        commands2 = [
            command_registry.CommandInfo("roll", "slash", "plugins/roll.py", "Roll dice", now),
            command_registry.CommandInfo("help2", "prefix", "plugins/help.py", "Help", now),
        ]
        command_registry.rebuild_registry(commands2)
        all_cmds = command_registry.get_all_commands()
        assert len(all_cmds) == 2
        names = {c.command_name for c in all_cmds}
        assert "ping" not in names
        assert "roll" in names
        assert "help2" in names

    def test_get_taken_names_groups_by_type(self, _use_temp_db) -> None:
        now = time.time()
        commands = [
            command_registry.CommandInfo("ping", "prefix", "plugins/ping.py", "", now),
            command_registry.CommandInfo("roll", "slash", "plugins/roll.py", "", now),
            command_registry.CommandInfo("ping", "slash", "plugins/ping2.py", "", now),
        ]
        command_registry.rebuild_registry(commands)
        taken = command_registry.get_taken_names()
        assert "ping" in taken["prefix"]
        assert "roll" in taken["slash"]
        assert "ping" in taken["slash"]

    def test_check_collisions_detects_slash_collision(self, _use_temp_db) -> None:
        now = time.time()
        existing = [
            command_registry.CommandInfo("roll", "slash", "plugins/roll.py", "", now),
        ]
        command_registry.rebuild_registry(existing)

        new_cmds = [
            command_registry.CommandInfo("roll", "slash", "plugins/roll2.py", "", now),
        ]
        collisions = command_registry.check_collisions(new_cmds)
        assert len(collisions) == 1
        assert "roll" in collisions[0]
        assert "slash" in collisions[0]

    def test_check_collisions_same_name_different_type_ok(self, _use_temp_db) -> None:
        now = time.time()
        existing = [
            command_registry.CommandInfo("ping", "prefix", "plugins/ping.py", "", now),
        ]
        command_registry.rebuild_registry(existing)

        new_cmds = [
            command_registry.CommandInfo("ping", "slash", "plugins/ping2.py", "", now),
        ]
        collisions = command_registry.check_collisions(new_cmds)
        assert collisions == []

    def test_check_collisions_no_collision(self, _use_temp_db) -> None:
        now = time.time()
        existing = [
            command_registry.CommandInfo("ping", "prefix", "plugins/ping.py", "", now),
        ]
        command_registry.rebuild_registry(existing)

        new_cmds = [
            command_registry.CommandInfo("roll", "slash", "plugins/roll.py", "", now),
        ]
        collisions = command_registry.check_collisions(new_cmds)
        assert collisions == []


# ---------------------------------------------------------------------------
# Directory scanning tests
# ---------------------------------------------------------------------------

class TestScanPluginsDirectory:
    def test_scans_all_py_files(self, tmp_path) -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugins_dir / "ping.py").write_text(
            'from discord.ext import commands\n'
            'class P:\n'
            '    @commands.command(name="ping")\n'
            '    async def ping(self, ctx): pass\n',
            encoding="utf-8",
        )
        (plugins_dir / "roll.py").write_text(
            'from discord import app_commands\n'
            'class R:\n'
            '    @app_commands.command(name="roll")\n'
            '    async def roll(self, interaction): pass\n',
            encoding="utf-8",
        )

        cmds = command_registry.scan_plugins_directory(str(plugins_dir))
        assert len(cmds) == 2
        names = {(c.command_name, c.command_type) for c in cmds}
        assert ("ping", "prefix") in names
        assert ("roll", "slash") in names

    def test_skips_init_py(self, tmp_path) -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "__init__.py").write_text(
            'from discord.ext import commands\n'
            'class P:\n'
            '    @commands.command(name="hidden")\n'
            '    async def hidden(self, ctx): pass\n',
            encoding="utf-8",
        )

        cmds = command_registry.scan_plugins_directory(str(plugins_dir))
        assert cmds == []

    def test_handles_empty_directory(self, tmp_path) -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        cmds = command_registry.scan_plugins_directory(str(plugins_dir))
        assert cmds == []

    def test_handles_nonexistent_directory(self) -> None:
        cmds = command_registry.scan_plugins_directory("/nonexistent/path")
        assert cmds == []
