"""Minimum capability-safety lint for agent tool functions.

ADR #30 layer-1: agent tool files (containing functions decorated with
``@tool`` / ``@agent.tool``) must NOT import a raw ``WalletProvider`` or
``EVMWalletProvider``. If those classes are imported at module scope, an
LLM-driven tool can capture an instance in its closure and bypass the
``X402Signer`` / scoped-signer convention that ADR #30 relies on.

Strategy (AST-only, no type inference):
1. Parse each Python source.
2. Collect any *@tool / @agent.tool* function definitions.
3. Collect any top-level imports of banned names
   (``WalletProvider`` / ``EVMWalletProvider`` / ``MPCWalletProvider``).
4. Flag (a) when a @tool argument is annotated with a banned name
   (precise; always wrong) **or** (b) when the file has at least one
   @tool function and imports a banned name at module scope (heuristic;
   the import implies a potential capability leak).

Bypass markers:
- Per-function: ``# capability-ok: <reason>`` on the def line.
- Per-file: ``# capability-ok: <reason>`` anywhere in the first 10 lines.

Usage::

    python tools/lint_capability.py [paths...]

Exit code 0 = clean; 1 = violations found; 2 = invocation error.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

DECORATOR_NAMES = {"tool", "agent_tool"}
DECORATOR_ATTRS = {"tool", "agent_tool"}  # e.g. @agent.tool
BANNED_NAMES = {"WalletProvider", "EVMWalletProvider", "MPCWalletProvider"}
BYPASS_MARKER = "capability-ok"


def _is_tool_decorator(dec: ast.expr) -> bool:
    """``@tool``, ``@something.tool``, ``@agent.tool()`` all count."""
    if isinstance(dec, ast.Name):
        return dec.id in DECORATOR_NAMES
    if isinstance(dec, ast.Attribute):
        return dec.attr in DECORATOR_ATTRS
    if isinstance(dec, ast.Call):
        return _is_tool_decorator(dec.func)
    return False


def _is_tool_func(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(_is_tool_decorator(d) for d in node.decorator_list)


def _file_has_bypass(text: str) -> bool:
    """File-level bypass marker in the first 10 lines."""
    for line in text.splitlines()[:10]:
        if BYPASS_MARKER in line:
            return True
    return False


def _def_line_has_bypass(text: str, lineno: int) -> bool:
    lines = text.splitlines()
    if lineno - 1 < len(lines) and BYPASS_MARKER in lines[lineno - 1]:
        return True
    return False


def _collect_banned_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Return [(lineno, name), ...] for banned classes imported at module scope."""
    out: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in BANNED_NAMES:
                    out.append((node.lineno, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                tail = alias.name.rsplit(".", 1)[-1]
                if tail in BANNED_NAMES:
                    out.append((node.lineno, alias.name))
    return out


def _arg_annotation_violations(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    for arg in func.args.args + func.args.kwonlyargs:
        if not arg.annotation:
            continue
        for node in ast.walk(arg.annotation):
            if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
                return f"argument {arg.arg!r} annotated as {node.id}"
            if isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
                return f"argument {arg.arg!r} annotated as ...{node.attr}"
    return None


def lint_file(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        return [f"{path}: read error: {e}"]
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: parse error: {e.msg}"]

    file_bypass = _file_has_bypass(text)

    tool_funcs = [n for n in ast.walk(tree) if _is_tool_func(n)]
    if not tool_funcs:
        return []  # file has no agent tools; not in scope of this lint

    out: list[str] = []

    # (a) per-arg precise check — always run, even with file-level bypass
    for func in tool_funcs:
        if _def_line_has_bypass(text, func.lineno):
            continue
        reason = _arg_annotation_violations(func)
        if reason:
            out.append(f"{path}:{func.lineno}: @tool {func.name}() — {reason}")

    # (b) module-level imports of banned names + presence of @tool
    # Suppress file-level violation if (i) explicit file bypass, or
    # (ii) every @tool function has its own per-function bypass marker.
    unbypassed_tools = [f for f in tool_funcs if not _def_line_has_bypass(text, f.lineno)]
    if not file_bypass and unbypassed_tools:
        banned_imports = _collect_banned_imports(tree)
        if banned_imports:
            names = ", ".join(sorted({n for _, n in banned_imports}))
            first_lineno = min(ln for ln, _ in banned_imports)
            tool_names = ", ".join(f.name for f in unbypassed_tools[:3])
            out.append(
                f"{path}:{first_lineno}: file imports {{ {names} }} at module scope "
                f"AND defines @tool function(s) ({tool_names}); "
                f"this enables closure-capture capability leak to LLM. "
                f"Move WalletProvider acquisition out of the tool file, or add "
                f"'# {BYPASS_MARKER}: <reason>' near the top to bypass."
            )

    return out


def iter_paths(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if root.is_file() and root.suffix in {".py", ".tmpl"}:
            yield root
        elif root.is_dir():
            for ext in ("*.py", "*.tmpl"):
                yield from root.rglob(ext)


def main(argv: list[str]) -> int:
    roots = [Path(p) for p in argv] if argv else [Path("bnbagent"), Path("examples"), Path("tests")]
    roots = [r for r in roots if r.exists()]
    if not roots:
        print("no input paths found", file=sys.stderr)
        return 2

    violations: list[str] = []
    scanned = 0
    for path in iter_paths(roots):
        if "lint_capability" in path.name or "__pycache__" in path.parts:
            continue
        scanned += 1
        violations.extend(lint_file(path))

    if violations:
        print(
            f"capability lint: {len(violations)} violation(s) in {scanned} file(s):",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"\nTo bypass a single function: '# {BYPASS_MARKER}: <reason>' on def line.\n"
            f"To bypass the entire file: '# {BYPASS_MARKER}: <reason>' in first 10 lines.",
            file=sys.stderr,
        )
        return 1
    print(f"capability lint: clean ({scanned} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
