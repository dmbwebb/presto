#!/usr/bin/env python3
"""Behavioural tests for tools/check_style.py on tests/fixtures/tools-deck."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from check_style import (  # noqa: E402
    acronym_definitions,
    acronym_findings,
    check_style as run_check_style,
    exit_code,
    format_steps,
    main,
)

FIXTURE = REPO / "tests" / "fixtures" / "tools-deck" / "index.html"


def info(index, classes=("content",), appendix=False):
    return {"index": index, "classes": list(classes), "isAppendix": appendix}


class CheckStyleFixtureTests(unittest.TestCase):
    """One real browser run over the fixture deck, then assertions per slide id."""

    @classmethod
    def setUpClass(cls):
        cls.report, cls.infos = run_check_style(str(FIXTURE))
        cls.index_by_id = {s["id"]: s["index"] for s in cls.infos if s["id"]}

    def findings(self, slide_id=None, rule=None, severity=None):
        index = self.index_by_id[slide_id] if slide_id else None
        return [
            f for f in self.report["findings"]
            if (slide_id is None or f["slide"] == index)
            and (rule is None or f["rule"] == rule)
            and (severity is None or f["severity"] == severity)
        ]

    def test_text_below_14px_is_an_error_including_scaled_svg_text(self):
        [error] = self.findings("tiny-text", "text-size", "error")
        texts = {e["text"]: e for e in error["detail"]["elements"]}
        self.assertEqual(12.0, texts["Tiny twelve pixel text"]["size"])
        # 20px SVG text in a 600-unit viewBox drawn 300px wide renders at 10px.
        self.assertAlmostEqual(10.0, texts["Shrunken figure label"]["size"], delta=0.2)
        self.assertEqual("svg", texts["Shrunken figure label"]["kind"])

    def test_text_between_14_and_20px_warns_unless_allowlisted(self):
        [warning] = self.findings("tiny-text", "text-size", "warning")
        texts = [e["text"] for e in warning["detail"]["elements"]]
        self.assertEqual(["Small sixteen pixel text"], texts)

    def test_appendix_floor_is_14px_and_only_warns(self):
        found = self.findings("app-small", "text-size")
        self.assertEqual(["warning"], [f["severity"] for f in found])
        self.assertEqual(["Appendix twelve pixel text"], [e["text"] for e in found[0]["detail"]["elements"]])

    def test_clipped_table_is_an_error(self):
        [clipped] = self.findings("clipped", "clipped", "error")
        self.assertGreaterEqual(clipped["detail"]["count"], 2)
        self.assertIn("cut off", clipped["detail"]["text"])

    def test_content_over_the_slide_number_is_an_error(self):
        [overlap] = self.findings("footer-overlap", "footer-overlap", "error")
        self.assertEqual("slide number", overlap["detail"]["zone"])

    def test_undefined_acronym_warns_and_defined_or_known_ones_do_not(self):
        flagged = {f["detail"]["acronym"] for f in self.findings(rule="acronym")}
        self.assertIn("XYZQ", flagged)
        self.assertNotIn("ITT", flagged)  # spelled out on first use
        self.assertNotIn("RCT", flagged)  # built-in list
        self.assertNotIn("BREAD", flagged)  # deck data-known-acronyms

    def test_low_resolution_image_warns(self):
        [low] = self.findings("lowres", "low-res-image", "warning")
        self.assertEqual([64, 36], low["detail"]["natural"])
        self.assertLess(low["detail"]["ratio"], 0.2)

    def test_dense_slide_and_long_bullets_warn(self):
        self.assertEqual(1, len(self.findings("dense", "words-slide", "warning")))
        [bullets] = self.findings("dense", "words-bullet", "warning")
        self.assertEqual(4, len(bullets["detail"]["bullets"]))

    def test_top_heavy_slide_warns_and_centred_slide_does_not(self):
        self.assertEqual(1, len(self.findings("top-heavy", "top-heavy", "warning")))
        self.assertEqual([], self.findings("clean"))

    def test_katex_error_is_an_error(self):
        [err] = self.findings("katex-error", "katex-error", "error")
        self.assertIn("ParseError", err["detail"]["detail"])

    def test_stale_data_steps_is_only_a_note(self):
        [note] = self.findings(rule="data-steps")
        self.assertEqual("info", note["severity"])
        self.assertEqual([{"slide": 2, "declared": "5", "engine": 3}], note["detail"]["stale"])

    def test_steps_come_from_the_engine(self):
        steps = {s["id"]: s["maxStep"] for s in self.infos if s["id"]}
        self.assertEqual(3, steps["ranged"])  # declared data-steps="5" is ignored
        self.assertEqual(3, steps["svg-steps"])  # steps inside the inlined SVG
        self.assertEqual(2, steps["table-steps"])  # rows of the loaded external table

    def test_reveal_each_list_gets_one_step_per_item(self):
        steps = {s["id"]: s["maxStep"] for s in self.infos if s["id"]}
        self.assertEqual(4, steps["reveal-each"])

    def test_errors_fail_and_strict_fails_on_warnings(self):
        self.assertEqual(1, exit_code(self.report, strict=False))
        clean = {"summary": {"errors": 0, "warnings": 2, "info": 1}}
        self.assertEqual(0, exit_code(clean, strict=False))
        self.assertEqual(1, exit_code(clean, strict=True))

    def test_json_and_slides_filter(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            main([str(FIXTURE), "--json", "--slides", "clean,top-heavy"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, caught.exception.code)  # only a warning on these two slides
        self.assertEqual({"clean", "top-heavy"}, {s["id"] for s in payload["slides"]})
        self.assertEqual(["top-heavy"], [f["rule"] for f in payload["findings"]])

    def test_strict_flag_turns_warnings_into_failure(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([str(FIXTURE), "--strict", "--slides", "top-heavy"])
        self.assertEqual(1, caught.exception.code)


class CheckStyleDeckScriptTests(unittest.TestCase):
    def test_font_shrinking_deck_script_is_reported_with_slides_and_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").symlink_to(REPO / "engine", target_is_directory=True)
            deck = root / "deck" / "index.html"
            deck.parent.mkdir()
            deck.write_text(
                """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="../engine/webslides.css">
<link rel="stylesheet" href="../engine/themes/academic.css">
<script defer src="../engine/webslides.js"></script>
<script>
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('section.slide').forEach(s => {
        if (s.scrollHeight > 720) s.style.fontSize = '15px';
    });
});
</script>
</head><body><div class="ws-deck">
<section class="slide content"><h2>Long</h2>""" + "<p>Line of text</p>" * 30 + """</section>
</div></body></html>""",
                encoding="utf-8",
            )
            report, _infos = run_check_style(str(deck))
        [script] = [f for f in report["findings"] if f["rule"] == "font-script"]
        self.assertEqual("warning", script["severity"])
        self.assertEqual([(6, 12)], [tuple(b) for b in script["detail"]["script_lines"]])
        self.assertEqual([0], [c["slideIndex"] for c in script["detail"]["changes"]])

    def test_failed_table_load_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").symlink_to(REPO / "engine", target_is_directory=True)
            deck = root / "deck" / "index.html"
            deck.parent.mkdir()
            deck.write_text(
                """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="../engine/webslides.css">
<script defer src="../engine/webslides.js"></script>
</head><body><div class="ws-deck">
<section class="slide content"><h2>Table</h2><div class="table-container" data-table-src="tables/missing.html"></div></section>
</div></body></html>""",
                encoding="utf-8",
            )
            report, _infos = run_check_style(str(deck))
        [err] = [f for f in report["findings"] if f["rule"] == "load-error"]
        self.assertEqual("error", err["severity"])
        self.assertEqual(0, err["slide"])
        self.assertIn("missing.html", err["message"])


def check_temp_deck(slides_html):
    """check_style on a throwaway deck that uses the repo engine and theme."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "engine").symlink_to(REPO / "engine", target_is_directory=True)
        deck = root / "deck" / "index.html"
        deck.parent.mkdir()
        deck.write_text(
            """<!DOCTYPE html><html><head><meta charset="UTF-8">
<link rel="stylesheet" href="../engine/webslides.css">
<link rel="stylesheet" href="../engine/themes/academic.css">
<script defer src="../engine/webslides.js"></script>
</head><body><div class="ws-deck">
""" + slides_html + """
</div></body></html>""",
            encoding="utf-8",
        )
        report, infos = run_check_style(str(deck))
    return report, {s["id"]: s["index"] for s in infos if s["id"]}


class CheckStyleTempDeckTests(unittest.TestCase):
    def test_failed_svg_load_is_one_error_and_its_error_box_is_not_linted(self):
        # Drawn 300px wide, the error box's 20px label renders below 14px, and
        # "SVG" in it is not spelled out: neither is the deck's problem.
        report, _ids = check_temp_deck(
            """<section class="slide content" id="fig"><h2>Missing figure</h2>
<img data-inline src="Figures/missing.svg" alt="Missing" style="width: 300px"></section>""")
        rules = [f["rule"] for f in report["findings"] if f["rule"] != "top-heavy"]
        self.assertEqual(["load-error"], rules, report["findings"])
        self.assertIn("missing.svg", report["findings"][0]["message"])

    def test_outline_slide_is_not_top_heavy(self):
        toc = "".join(
            f'<div class="toc-item"><span class="toc-number">{n}</span>'
            f'<span class="toc-label">Part {n}</span></div>'
            for n in range(1, 4)
        )
        report, ids = check_temp_deck(
            f'<section class="slide content outline" id="outline"><h2>Outline</h2>{toc}</section>\n'
            f'<section class="slide content" id="plain"><h2>Same items, plain slide</h2>{toc}</section>')
        top_heavy = {f["slide"] for f in report["findings"] if f["rule"] == "top-heavy"}
        self.assertNotIn(ids["outline"], top_heavy)
        self.assertIn(ids["plain"], top_heavy, "the control slide shows the check is live")

    def test_content_under_the_pinned_appendix_or_back_buttons_is_an_error(self):
        report, ids = check_temp_deck(
            """<section class="slide content" id="covered"><h2>Covered button</h2>
<p>Short content</p>
<p style="position: absolute; right: 60px; bottom: 30px; margin: 0">Text running under the button</p>
<div class="header-links"><a class="ws-appendix-link" href="#app">Robustness</a></div></section>
<section class="slide content" id="clear"><h2>Clear button</h2>
<p>Short content</p>
<div class="header-links"><a class="ws-appendix-link" href="#app">Robustness</a></div></section>
<section class="slide appendix" id="app"><h2>Appendix</h2>
<p style="position: absolute; right: 60px; bottom: 30px; margin: 0">Appendix text under the back link</p>
<a class="ws-back-link" href="#">Back</a></section>""")
        overlaps = [f for f in report["findings"] if f["rule"] == "button-overlap"]
        self.assertEqual({ids["covered"], ids["app"]}, {f["slide"] for f in overlaps}, report["findings"])
        self.assertTrue(all(f["severity"] == "error" for f in overlaps))
        by_slide = {f["slide"]: f for f in overlaps}
        self.assertEqual("appendix buttons", by_slide[ids["covered"]]["detail"]["zone"])
        self.assertIn("Text running under", by_slide[ids["covered"]]["detail"]["text"])
        self.assertEqual("back button", by_slide[ids["app"]]["detail"]["zone"])
        self.assertEqual(1, exit_code(report, strict=False))


class CheckStyleUnitTests(unittest.TestCase):
    def test_acronym_definition_patterns(self):
        self.assertTrue(acronym_definitions("we use intent to treat (ITT) estimates", "ITT"))
        self.assertTrue(acronym_definitions("ITT (intent to treat) estimates", "ITT"))
        self.assertTrue(acronym_definitions("ITT = intent to treat", "ITT"))
        self.assertFalse(acronym_definitions("ITT (0.12) estimates", "ITT"))
        self.assertFalse(acronym_definitions("the ITT is large", "ITT"))

    def test_acronyms_defined_on_an_earlier_slide_are_not_flagged_later(self):
        infos = [info(0), info(1), info(2)]
        texts = ["Average treatment effect (ATE) basics", "The ATE and the LATE", "LATE again"]
        found = acronym_findings(infos, texts, [], {0, 1, 2})
        self.assertEqual([("LATE", 1)], [(f["detail"]["acronym"], f["slide"]) for f in found])
        self.assertEqual([2, 3], found[0]["detail"]["slides"])

    def test_acronyms_on_title_and_appendix_slides_are_not_flagged(self):
        infos = [info(0, ("title",)), info(1, appendix=True)]
        found = acronym_findings(infos, ["NBER SI 2026", "MHM table"], [], {0, 1})
        self.assertEqual([], found)

    def test_plurals_roman_numerals_and_emphasis_are_not_acronyms(self):
        found = acronym_findings([info(0)], ["RCTs in Phase III are NOT cheap"], [], {0})
        self.assertEqual([], found)

    def test_deck_known_acronyms_are_case_insensitive(self):
        found = acronym_findings([info(0)], ["CSAE and MHM"], ["csae", "MHM"], {0})
        self.assertEqual([], found)

    def test_format_steps(self):
        self.assertEqual("", format_steps([0, 1, 2], 0))
        self.assertEqual("steps 0-3", format_steps([0, 1, 2, 3], 3))
        self.assertEqual("step 3", format_steps([3], 3))
        self.assertEqual("steps 1-3", format_steps([1, 2, 3], 3))
        self.assertEqual("step 1", format_steps([1], 3))
        self.assertEqual("steps 0,2", format_steps([0, 2], 3))

    def test_main_reports_missing_deck_without_traceback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            main(["/nonexistent/deck/index.html"])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("not found", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
