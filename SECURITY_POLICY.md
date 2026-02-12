# Turbot Security Policy

This document is injected verbatim into every Claude code-generation prompt.
It defines what plugins may and may not do, and best practices for all generated code.

---

## Plugin Forbidden List

Plugins MUST NOT use any of the following. The AST scanner (`policy.py`) will
reject PRs that violate these rules.

### Forbidden Imports

```
os, subprocess, sys, shutil, importlib, ctypes, pathlib, signal, socket
```

### Forbidden Builtins

```
exec(), eval(), compile(), open(), __import__(), breakpoint()
```

### Forbidden Dunder Access

```
__subclasses__, __globals__, __builtins__, __code__, __class__
```

---

## Plugin Allowed List

### Network (via PluginContext helpers preferred)

```
aiohttp, requests, urllib
```

### Safe Standard Library

```
json, re, datetime, collections, math, hashlib, base64, enum,
dataclasses, typing, functools, itertools, string, textwrap,
logging, copy, random, uuid, html, urllib.parse
```

### Discord (via TurbotPlugin)

```
discord, discord.ext.commands
```

---

## Plugin Structure Template

Every plugin MUST follow this structure:

```python
from discord.ext import commands
from plugin_api import TurbotPlugin

class MyPlugin(TurbotPlugin):
    """Short description of what this plugin does."""

    @commands.command(name="mycommand")
    async def my_command(self, ctx: commands.Context) -> None:
        """Help text for the command."""
        await ctx.send("Response here")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MyPlugin(bot))
```

### Key Rules

- Extend `TurbotPlugin`, NOT `commands.Cog` directly
- Use `self.turbot` (a `PluginContext`) for bot API — NOT `self.bot`
- Use `ctx` for command replies
- Module-level `async def setup(bot)` is required for discord.py extension loading
- Plugin files live in `plugins/` only
- Use `self.turbot.store_get(key)` / `self.turbot.store_set(key, value)` for persistence
- NEVER use raw file I/O (`open()`, `pathlib`, etc.) — the store API handles it safely

---

## Best Practices for ALL Code Generation

These apply to both plugin and core code:

1. **Type hints**: All function parameters and return types annotated
2. **Return annotations**: Every function/method has `-> ReturnType`
3. **File encoding**: All `open()` calls use `encoding="utf-8"` (PEP 597)
4. **Error handling**: Use specific exceptions — no bare `except:`
5. **Docstrings**: All classes and public functions have docstrings
6. **Modern Python**: Python 3.12+, PEP 604 unions (`X | Y`), no `Optional`

---

## Core Change Rules

Changes to files outside `plugins/` are **core changes** and:

- Are flagged with "CORE CHANGE" in the PR body
- Trigger an admin channel warning
- Require human review before merge
- Should only be requested via `bot improvement:` prefix

---

## Storage Best Practices

- Use `self.turbot.store_get(key)` to read persisted data
- Use `self.turbot.store_set(key, value)` to write persisted data
- Data is stored as JSON in `data/<plugin_name>/` (isolated per plugin)
- Values must be JSON-serializable
- NEVER use raw file I/O — the store API handles file operations internally
