"""Regression guard: two broad message handlers must never share a dispatcher group.

WHY THIS EXISTS
═══════════════
Pyrogram / Kurigram dispatch update handlers group by group (ascending). Inside
one group it runs the FIRST handler whose filter passes and then breaks out of
that group — unless the handler raises `ContinuePropagation`. So two handlers
registered in the same group with a filter as broad as
`filters.group & ~filters.service` are mutually exclusive: the one that was
imported first (plugins load alphabetically) silently eats every message.

This test parses the source (no Telegram credentials, no network) and fails if
any dispatcher group ever again holds more than one catch-all group-message
handler.
"""
from __future__ import annotations

import ast
import os
from collections import defaultdict

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "melody")

# Filters that match (almost) every ordinary group message.
BROAD = {
    "filters.group & ~filters.service",
    "~filters.service & filters.group",
    "filters.group",
}


def _group_kwarg(call: ast.Call) -> int:
    for kw in call.keywords:
        if kw.arg == "group":
            return ast.literal_eval(kw.value)
    return 0


def _collect() -> dict:
    found = defaultdict(list)
    for base, _dirs, files in os.walk(ROOT):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for node in ast.walk(tree):
                for dec in getattr(node, "decorator_list", []):
                    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                        continue
                    client = getattr(dec.func.value, "id", None)
                    if client not in ("bot", "assistant") or dec.func.attr != "on_message":
                        continue
                    if not dec.args:
                        continue
                    expr = ast.unparse(dec.args[0])
                    if expr not in BROAD:
                        continue
                    found[(client, _group_kwarg(dec))].append(f"{path}:{node.name}")
    return found


def test_no_two_broad_handlers_share_a_dispatcher_group():
    clashes = {key: names for key, names in _collect().items() if len(names) > 1}
    assert not clashes, (
        "These catch-all message handlers share a dispatcher group, so only the "
        f"first one will ever run: {clashes}"
    )
