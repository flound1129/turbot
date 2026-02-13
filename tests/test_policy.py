"""Tests for the AST-based security policy scanner."""

import pytest

import policy


class TestBannedImports:
    """Each banned import should be detected."""

    @pytest.mark.parametrize("module", [
        "os", "subprocess", "sys", "shutil", "importlib",
        "ctypes", "pathlib", "signal", "socket",
    ])
    def test_import_banned_module(self, module: str) -> None:
        source = f"import {module}\n"
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-import"
        assert module in result.violations[0].detail

    @pytest.mark.parametrize("module", [
        "os", "subprocess", "sys", "shutil", "importlib",
        "ctypes", "pathlib", "signal", "socket",
    ])
    def test_from_import_banned_module(self, module: str) -> None:
        source = f"from {module} import something\n"
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-import"

    def test_import_os_path(self) -> None:
        source = "import os.path\n"
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-import"

    def test_from_os_path_import(self) -> None:
        source = "from os.path import join\n"
        result = policy.scan_source(source)
        assert not result.ok


class TestBannedBuiltins:
    """Each banned builtin call should be detected."""

    @pytest.mark.parametrize("builtin", [
        "exec", "eval", "compile", "open", "__import__", "breakpoint",
    ])
    def test_call_banned_builtin(self, builtin: str) -> None:
        source = f"x = {builtin}('something')\n"
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-builtin"
        assert builtin in result.violations[0].detail


class TestBannedDunderAccess:
    """Dunder attribute access should be detected."""

    @pytest.mark.parametrize("dunder", [
        "__subclasses__", "__globals__", "__builtins__", "__code__", "__class__",
    ])
    def test_dunder_access(self, dunder: str) -> None:
        source = f"x = obj.{dunder}\n"
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-dunder"
        assert dunder in result.violations[0].detail


class TestAllowedImports:
    """Allowed imports should pass cleanly."""

    @pytest.mark.parametrize("module", [
        "aiohttp", "requests", "json", "re", "datetime",
        "collections", "math", "discord", "discord.ext.commands",
    ])
    def test_allowed_import(self, module: str) -> None:
        source = f"import {module}\n"
        result = policy.scan_source(source)
        assert result.ok


class TestSyntaxErrors:
    def test_syntax_error_reported(self) -> None:
        source = "def foo(\n"
        result = policy.scan_source(source, "bad.py")
        assert not result.ok
        assert result.violations[0].rule == "syntax-error"
        assert result.path == "bad.py"


class TestMultipleViolations:
    def test_multiple_violations_in_one_file(self) -> None:
        source = "import os\nimport subprocess\nexec('bad')\n"
        result = policy.scan_source(source)
        assert len(result.violations) >= 3


class TestScanChanges:
    def test_only_scans_plugins_directory(self) -> None:
        changes = [
            {"path": "bot.py", "action": "modify", "content": "import os\n"},
            {"path": "plugins/bad.py", "action": "create", "content": "import os\n"},
        ]
        results = policy.scan_changes(changes)
        assert len(results) == 1
        assert results[0].path == "plugins/bad.py"

    def test_skips_deletes(self) -> None:
        changes = [
            {"path": "plugins/old.py", "action": "delete", "content": ""},
        ]
        results = policy.scan_changes(changes)
        assert results == []

    def test_skips_non_py_files(self) -> None:
        changes = [
            {"path": "plugins/readme.md", "action": "create", "content": "import os\n"},
        ]
        results = policy.scan_changes(changes)
        assert results == []

    def test_clean_plugin_passes(self) -> None:
        changes = [
            {
                "path": "plugins/good.py",
                "action": "create",
                "content": "import json\nimport re\n",
            },
        ]
        results = policy.scan_changes(changes)
        assert results == []

    def test_returns_violations_for_bad_plugin(self) -> None:
        changes = [
            {
                "path": "plugins/evil.py",
                "action": "create",
                "content": "import os\nexec('hack')\n",
            },
        ]
        results = policy.scan_changes(changes)
        assert len(results) == 1
        assert len(results[0].violations) >= 2


class TestDynamicDunderAccess:
    """Tests for getattr/setattr/delattr with banned dunder strings."""

    def test_getattr_with_banned_dunder(self) -> None:
        source = 'x = getattr(obj, "__globals__")\n'
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-dunder-access"

    def test_setattr_with_banned_dunder(self) -> None:
        source = 'setattr(obj, "__code__", val)\n'
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-dunder-access"

    def test_delattr_with_banned_dunder(self) -> None:
        source = 'delattr(obj, "__class__")\n'
        result = policy.scan_source(source)
        assert not result.ok
        assert result.violations[0].rule == "banned-dunder-access"

    def test_getattr_with_safe_string(self) -> None:
        source = 'x = getattr(obj, "name")\n'
        result = policy.scan_source(source)
        assert result.ok

    def test_getattr_with_variable(self) -> None:
        """Dynamic access via variable can't be caught statically — should pass."""
        source = 'attr = "__globals__"\nx = getattr(obj, attr)\n'
        result = policy.scan_source(source)
        # Only the variable assignment is visible, not the getattr target
        assert result.ok


class TestScanResult:
    def test_ok_property_true_when_no_violations(self) -> None:
        result = policy.ScanResult(path="test.py", violations=[])
        assert result.ok is True

    def test_ok_property_false_when_violations(self) -> None:
        result = policy.ScanResult(
            path="test.py",
            violations=[policy.Violation(1, 0, "test", "test")],
        )
        assert result.ok is False
