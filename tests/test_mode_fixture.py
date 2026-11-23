#!/usr/bin/env python3
import unittest
from pathlib import Path


MODE_TESTS = Path(__file__).resolve().parents[1] / "tests" / "mode-tests.html"


class ModeFixtureTests(unittest.TestCase):
    def test_invalid_mode_param_expectation_matches_engine_normalization(self):
        html = MODE_TESTS.read_text(encoding="utf-8")
        self.assertIn("allowedModes", html)
        self.assertIn("allowedModes.includes(rawMode) ? rawMode : 'normal'", html)

    def test_numeric_expectations_use_strict_integer_parsing(self):
        html = MODE_TESTS.read_text(encoding="utf-8")
        self.assertIn("function parseExpectedInteger", html)
        self.assertIn("Number.isInteger", html)
        self.assertNotIn("parseInt(", html)


if __name__ == "__main__":
    unittest.main()
