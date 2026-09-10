from __future__ import annotations
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "melody/plugins"
for path in sorted(root.rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"))
    handlers = []
    broad = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                broad += 1
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decs = [ast.unparse(d) for d in node.decorator_list]
        if any("bot.on_" in d for d in decs):
            commands = []
            for d in node.decorator_list:
                rendered = ast.unparse(d)
                if "filters.command" in rendered:
                    commands.append(rendered)
            handlers.append((node.name, "async" if isinstance(node, ast.AsyncFunctionDef) else "sync", commands, decs))
    print(f"{path.relative_to(root.parent.parent)} handlers={len(handlers)} broad_excepts={broad}")
    for name, kind, commands, _decs in handlers:
        print(f"  {kind:5} {name:32} commands={commands}")
