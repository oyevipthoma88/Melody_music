#!/usr/bin/env python3
"""Static verification for command aliases and Telegram filter scopes."""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
commands: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
parse_errors: list[tuple[Path, Exception]] = []


def scope_for(dec: ast.Call) -> str:
    parts: list[str] = []
    if dec.keywords:
        for kw in dec.keywords:
            if kw.arg == "group":
                parts.append(f"handler-group={ast.unparse(kw.value)}")
    if dec.args:
        expression = ast.unparse(dec.args[0])
        for scope in ("private", "group", "channel", "supergroup"):
            if f"filters.{scope}" in expression:
                parts.append(scope)
    return ",".join(parts) or "any"


for path in sorted((ROOT / "melody/plugins").rglob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        parse_errors.append((path, exc))
        continue
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if dec.func.attr != "on_message" or not dec.args:
                continue
            command_calls = [
                call for call in ast.walk(dec.args[0])
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "command"
                and call.args
            ]
            scope = scope_for(dec)
            for call in command_calls:
                value = call.args[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    names = [value.value]
                elif isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                    names = [
                        item.value for item in value.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ]
                else:
                    names = []
                for name in names:
                    commands[name.lower()].append((str(path.relative_to(ROOT)), node.name, scope))

print(f"command_aliases={len(commands)}")
print(f"message_handlers={sum(len(items) for items in commands.values())}")
print(f"parse_errors={len(parse_errors)}")
for name in sorted(commands):
    owners = ", ".join(f"{path}:{function} [{scope}]" for path, function, scope in commands[name])
    print(f"/{name} -> {owners}")

collisions = {
    name: items for name, items in commands.items()
    if len({scope for _path, _function, scope in items}) < len(items)
}
print(f"scope_collisions={len(collisions)}")
for name, items in sorted(collisions.items()):
    print(f"COLLISION /{name}: {items}")

if parse_errors or collisions:
    raise SystemExit(1)
