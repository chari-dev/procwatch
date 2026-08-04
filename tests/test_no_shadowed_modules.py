"""No local variable may shadow an imported module.

A handler assigned `live = space.files_in(...)` inside api_get. That one line
made `live` local to the *entire* function, so `live.snapshot()` on a
different endpoint -- /api/now, hundreds of lines away -- raised
UnboundLocalError and closed the connection with no response.

Nothing caught it. The unit tests import the modules directly rather than
going through the request handler, the page's own harness stubs fetch, and the
symptom appeared on an endpoint whose code had not been touched. It is exactly
the shape of fault that is cheap to detect statically and expensive to find by
hand, so it is detected statically.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "procwatch")


def module_names():
    return {name[:-3] for name in os.listdir(PACKAGE)
            if name.endswith(".py") and name != "__init__.py"}


def imported_in(tree):
    """Module names this file pulls in with `from . import x` or `import x`."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.asname or alias.name.split(".")[0])
    return found


def assigned_names(func):
    """Every name bound anywhere in a function body, at any depth.

    Depth matters: Python decides a name is local from any binding in the
    function, including one inside an `if` that never runs.
    """
    out = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                        out.add(sub.id)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            if isinstance(node.target, ast.Name):
                out.add(node.target.id)
        elif isinstance(node, (ast.For, ast.comprehension)):
            target = getattr(node, "target", None)
            if target is not None:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for sub in ast.walk(node.optional_vars):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
    return out


class NoShadowedModulesTest(unittest.TestCase):
    def test_no_function_binds_a_module_name(self):
        modules = module_names()
        problems = []
        for name in sorted(modules):
            path = os.path.join(PACKAGE, name + ".py")
            with open(path) as handle:
                tree = ast.parse(handle.read(), filename=path)
            available = imported_in(tree) & modules
            if not available:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # A parameter of the same name is a deliberate choice and is
                # local from the first line, so it cannot surprise anyone.
                params = {a.arg for a in node.args.args + node.args.kwonlyargs}
                clash = (assigned_names(node) & available) - params
                for bad in sorted(clash):
                    problems.append(
                        "%s.py: %s() assigns to '%s', which is an imported "
                        "module in this file -- every use of %s.* in that "
                        "function becomes UnboundLocalError"
                        % (name, node.name, bad, bad))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
