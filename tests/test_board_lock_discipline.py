"""Every writer of `seen_jobs.json` must hold the board lock.

The board's HTTP handler and job import/collection tools can write that file. A writer that forgets the
lock does not fail loudly; it silently reverts whatever another writer did
between its read and its write, and the thing most likely to be lost is a status
you just set by hand.

So this is a structural guard rather than a behavioural one: it reads the source
and fails if a `save_seen()` call is not lexically inside a `board_lock()` block.
A new tool that writes the board has to opt into the lock to get past CI.
"""

import ast
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"

# `save_seen` itself lives here, and `board_lock` is defined here too.
WRITERS = ("save_seen",)


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _guarded_by_board_lock(stack):
    """Is any enclosing `with` statement a board_lock()?"""
    for node in stack:
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and _call_name(expr) == "board_lock":
                return True
    return False


def unguarded_writes(path):
    """[(function, line)] for every board write outside a board_lock() block."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def walk(node, stack, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call) and _call_name(node) in WRITERS:
            # The definition of save_seen is not a call site, and the board's
            # save() helper is called from handlers that hold the lock.
            if not _guarded_by_board_lock(stack):
                found.append((func, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, stack + [node], func)

    walk(tree, [], "<module>")
    return found


class BoardLockDisciplineTest(unittest.TestCase):
    # board.state.save() is a one-line helper; every caller holds the lock, and
    # the assertion below covers those callers.
    ALLOWED = {("tools/board/state.py", "save")}

    def test_every_board_write_is_inside_board_lock(self):
        offenders = []
        # rglob, not glob: the board server moved into tools/board/ and a guard
        # that only reads tools/*.py would have stopped covering it without
        # failing - the worst way for a structural test to break.
        for path in sorted(TOOLS.rglob("*.py")):
            rel = "tools/" + str(path.relative_to(TOOLS))
            for func, line in unguarded_writes(path):
                if (rel, func) in self.ALLOWED:
                    continue
                offenders.append("%s:%d in %s()" % (rel, line, func))
        self.assertEqual(
            offenders, [],
            "these write seen_jobs.json without holding jobs_md.board_lock(); a "
            "concurrent board or fetch will silently lose one side's changes:\n  "
            + "\n  ".join(offenders))

    def test_the_guard_can_actually_fail(self):
        """A guard that cannot fail is not a guard."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            bad.write_text("def go(seen):\n    save_seen(seen)\n", encoding="utf-8")
            self.assertEqual(unguarded_writes(bad), [("go", 2)])
            good = Path(tmp) / "good.py"
            good.write_text("def go(seen):\n    with board_lock():\n        save_seen(seen)\n",
                            encoding="utf-8")
            self.assertEqual(unguarded_writes(good), [])


if __name__ == "__main__":
    unittest.main()
