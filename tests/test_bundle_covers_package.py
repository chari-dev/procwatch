"""The single-file build must carry every module the package has.

MODULES in tools/bundle.py is written by hand, and nothing used to notice when
a new module was added to procwatch/ without being listed. The checkout keeps
working -- it imports from the package -- so the failure only appears in the
generated procwatch.py, which is the copy the sampler, the installer and the
menu bar app all actually run. It surfaces as `ImportError: cannot import name
'x' from 'procwatch' (unknown location)` at the first tick after release.

This is the guard for that: it fails at development time instead.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))


class BundleCoversPackageTest(unittest.TestCase):
    def package_modules(self):
        folder = os.path.join(ROOT, "procwatch")
        return {name[:-3] for name in os.listdir(folder)
                if name.endswith(".py") and name != "__init__.py"}

    def test_every_module_is_listed_in_the_bundle(self):
        import bundle
        missing = self.package_modules() - set(bundle.MODULES)
        self.assertEqual(
            missing, set(),
            "these modules exist in procwatch/ but are missing from "
            "tools/bundle.py MODULES, so the single-file build cannot "
            "import them: %s" % ", ".join(sorted(missing)))

    def test_the_bundle_lists_nothing_that_does_not_exist(self):
        import bundle
        extra = set(bundle.MODULES) - self.package_modules()
        self.assertEqual(extra, set(),
                         "listed in tools/bundle.py but not in procwatch/: %s"
                         % ", ".join(sorted(extra)))

    def test_dependency_order_holds(self):
        """A module may only import ones already inlined above it.

        The list's ordering is load-bearing -- the generated file execs each
        module in sequence -- and getting it wrong is another failure that
        only shows up in the built copy.
        """
        import ast
        import bundle
        position = {name: i for i, name in enumerate(bundle.MODULES)}
        problems = []
        for name in bundle.MODULES:
            path = os.path.join(ROOT, "procwatch", name + ".py")
            with open(path) as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                # Only module-level imports have to be ordered. An import
                # inside a function runs long after everything is installed,
                # which is how db.init_schema can reach modules below it.
                if not isinstance(node, ast.ImportFrom) or node.level != 1:
                    continue
                if not any(isinstance(parent, ast.Module)
                           for parent in [tree]) or node.col_offset != 0:
                    continue
                for alias in node.names:
                    target = alias.name
                    if target in position and position[target] > position[name]:
                        problems.append("%s imports %s, which is inlined after it"
                                        % (name, target))
        self.assertEqual(problems, [], "; ".join(problems))


if __name__ == "__main__":
    unittest.main()
