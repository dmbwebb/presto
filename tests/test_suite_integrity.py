#!/usr/bin/env python3
import ast
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
PRESTO_FEATURES = TESTS_DIR / "test_presto_features.swift"


class TestSuiteIntegrityTests(unittest.TestCase):
    def test_unittest_method_names_are_unique_within_each_class(self):
        duplicates = []
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                seen = {}
                for child in node.body:
                    if not isinstance(child, ast.FunctionDef) or not child.name.startswith("test"):
                        continue
                    first_line = seen.get(child.name)
                    if first_line is not None:
                        duplicates.append(
                            f"{path.name}:{node.name}.{child.name} lines {first_line} and {child.lineno}"
                        )
                    else:
                        seen[child.name] = child.lineno

        self.assertEqual([], duplicates)

    def test_presto_feature_runner_exits_nonzero_on_failures(self):
        source = PRESTO_FEATURES.read_text(encoding="utf-8")

        self.assertIn("let exitCode: Int32 = failed == 0 ? 0 : 1", source)
        self.assertIn("exit(exitCode)", source)
        self.assertIn("import Darwin", source)


if __name__ == "__main__":
    unittest.main()
