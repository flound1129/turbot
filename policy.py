"""AST-based static analysis scanner for Turbot plugin security policy.

Scans Python source for forbidden imports, builtins, and dunder attribute access
before any PR is created.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


BANNED_IMPORTS: frozenset[str] = frozenset({
    "os", "subprocess", "sys", "shutil", "importlib",
    "ctypes", "pathlib", "signal", "socket",
})

BANNED_BUILTINS: frozenset[str] = frozenset({
    "exec", "eval", "compile", "open", "__import__", "breakpoint",
})

BANNED_DUNDER_ATTRS: frozenset[str] = frozenset({
    "__subclasses__", "__globals__", "__builtins__", "__code__", "__class__",
})


@dataclass
class Violation:
    """A single policy violation found in source code."""

    line: int
    col: int
    rule: str
    detail: str


@dataclass
class ScanResult:
    """Result of scanning a single file."""

    path: str
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if no violations were found."""
        return len(self.violations) == 0


class _PolicyVisitor(ast.NodeVisitor):
    """AST visitor that collects policy violations."""

    def __init__(self) -> None:
        self.violations: list[Violation] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in BANNED_IMPORTS:
                self.violations.append(Violation(
                    line=node.lineno,
                    col=node.col_offset,
                    rule="banned-import",
                    detail=f"Import of '{alias.name}' is forbidden in plugins",
                ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            top = node.module.split(".")[0]
            if top in BANNED_IMPORTS:
                self.violations.append(Violation(
                    line=node.lineno,
                    col=node.col_offset,
                    rule="banned-import",
                    detail=f"Import from '{node.module}' is forbidden in plugins",
                ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name: str | None = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name and name in BANNED_BUILTINS:
            self.violations.append(Violation(
                line=node.lineno,
                col=node.col_offset,
                rule="banned-builtin",
                detail=f"Call to '{name}()' is forbidden in plugins",
            ))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in BANNED_DUNDER_ATTRS:
            self.violations.append(Violation(
                line=node.lineno,
                col=node.col_offset,
                rule="banned-dunder",
                detail=f"Access to '{node.attr}' is forbidden in plugins",
            ))
        self.generic_visit(node)


def scan_source(source: str, path: str = "<string>") -> ScanResult:
    """Scan Python source code for policy violations.

    Handles syntax errors gracefully by reporting them as violations.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return ScanResult(path=path, violations=[Violation(
            line=exc.lineno or 1,
            col=exc.offset or 0,
            rule="syntax-error",
            detail=f"Syntax error: {exc.msg}",
        )])

    visitor = _PolicyVisitor()
    visitor.visit(tree)
    return ScanResult(path=path, violations=visitor.violations)


def scan_changes(changes: list[dict[str, str]]) -> list[ScanResult]:
    """Scan a list of file changes, only checking files under ``plugins/``.

    Skips deletes and non-``.py`` files.  Returns a list of
    :class:`ScanResult` (one per scanned file).
    """
    results: list[ScanResult] = []
    for change in changes:
        path = change.get("path", "")
        action = change.get("action", "")

        # Only scan plugin files
        if not path.startswith("plugins/"):
            continue
        # Skip deletes and non-Python files
        if action == "delete":
            continue
        if not path.endswith(".py"):
            continue

        content = change.get("content", "")
        result = scan_source(content, path)
        if not result.ok:
            results.append(result)
    return results
