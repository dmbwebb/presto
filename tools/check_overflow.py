#!/usr/bin/env python3
"""Mechanically check a WebSlides deck for content overflow.

Renders the deck in headless Chromium via Playwright, walks through every
slide at every reveal step, and measures the actual rendered position of
each descendant element relative to the 1280×720 slide box. For each slide
it reports the worst step: the pixel overflow in each direction and the
specific culprit elements (tag, class, text snippet) that extend past the
bounds.

Unlike a static HTML scan, this catches overflow caused by wrapped text,
fitted tables, grids, flex layouts, SVG reveals, data-class layout changes,
and anything else whose height depends on the browser's actual layout
engine.

Usage:
    python3 tools/check_overflow.py decks/<deck>/index.html
    python3 tools/check_overflow.py decks/<deck>/index.html --all       # include appendix slides
    python3 tools/check_overflow.py decks/<deck>/index.html --verbose   # up to 10 culprits per slide
    python3 tools/check_overflow.py decks/<deck>/index.html --json      # machine-readable output
    python3 tools/check_overflow.py decks/<deck>/index.html --strict    # exit 1 if any overflow

Requires: pip install playwright && playwright install chromium
(``python3 tools/check_overflow.py`` re-runs itself under ~/.venvs/webslides-export
when the current interpreter lacks Playwright.)
"""

import argparse
import asyncio
import json
import sys

if __name__ == "__main__":
    from _common import ensure_tool_python

    ensure_tool_python(("playwright",))

from _common import (  # noqa: E402
    DECK_STRUCTURE_JS,
    LOAD_ERRORS_JS,
    SLIDE_HEIGHT,
    SLIDE_INFO_JS,
    SLIDE_WIDTH,
    build_deck_url,
    check_deck_file,
    find_webslides_root,
    format_load_error,
    raise_for_page_errors,
    resolve_deck_path,
    start_server,
    validate_browser_deck_structure,
    wait_for_deck_ready,
)

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    PlaywrightError = PlaywrightTimeoutError = RuntimeError


# JS that runs inside the page. Loops through every slide × every reveal
# step, navigates via WS.goToSlide, and measures overflow. Does the entire
# traversal in a single evaluate() call to avoid Python↔browser round-trip
# overhead. Temporarily strips each slide's transform and overflow:hidden
# so getBoundingClientRect returns intrinsic layout coordinates. Step counts
# come from the engine (maxSteps[i], see _common.SLIDE_INFO_JS).
MEASURE_JS = r"""
(maxSteps) => {
    const NOMINAL_W = 1280;
    const NOMINAL_H = 720;
    const TOL = 1;  // subpixel slack

    function measureSlide(slide) {
        const origTransform = slide.style.transform;
        const origOverflow = slide.style.overflow;
        slide.style.transform = 'none';
        slide.style.overflow = 'visible';
        void slide.offsetHeight;  // force layout

        const sRect = slide.getBoundingClientRect();
        let maxBottom = 0;
        let maxRight = 0;
        const items = [];

        const walker = slide.querySelectorAll('*');
        for (const el of walker) {
            const tag = el.tagName;
            if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'BR') continue;
            if (el.classList && el.classList.contains('notes')) continue;

            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') continue;

            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;

            const bottom = r.bottom - sRect.top;
            const right = r.right - sRect.left;
            if (bottom > maxBottom) maxBottom = bottom;
            if (right > maxRight) maxRight = right;

            if (bottom > NOMINAL_H + TOL || right > NOMINAL_W + TOL) {
                let text = (el.innerText || el.textContent || '')
                    .trim().replace(/\s+/g, ' ');
                if (text.length > 70) text = text.slice(0, 67) + '...';

                let chain = [];
                let p = el;
                let depth = 0;
                while (p && p !== slide && depth < 8) {
                    let seg = p.tagName.toLowerCase();
                    if (p.className && typeof p.className === 'string') {
                        const cls = p.className.trim().split(/\s+/).slice(0, 2).join('.');
                        if (cls) seg += '.' + cls;
                    }
                    chain.unshift(seg);
                    p = p.parentElement;
                    depth++;
                }

                items.push({
                    tag: tag.toLowerCase(),
                    cls: (el.className || '').toString().trim().slice(0, 80),
                    id: el.id || '',
                    text: text,
                    bottom: Math.round(bottom),
                    right: Math.round(right),
                    bOver: Math.max(0, Math.round(bottom - NOMINAL_H)),
                    rOver: Math.max(0, Math.round(right - NOMINAL_W)),
                    childCount: el.children.length,
                    chain: chain.join(' > '),
                });
            }
        }

        slide.style.transform = origTransform;
        slide.style.overflow = origOverflow;

        return {
            vOverflow: Math.max(0, Math.round(maxBottom - NOMINAL_H)),
            hOverflow: Math.max(0, Math.round(maxRight - NOMINAL_W)),
            maxBottom: Math.round(maxBottom),
            maxRight: Math.round(maxRight),
            items: items,
        };
    }

    const slides = WS.slides;
    const results = [];

    for (let idx = 0; idx < slides.length; idx++) {
        const slide = slides[idx];
        if (!slide) continue;
        const maxStep = maxSteps[idx] || 0;

        const titleEl = slide.querySelector('h1, h2');
        const title = titleEl ? (titleEl.textContent || '').trim().slice(0, 100) : '';
        const isAppendix = slide.classList.contains('appendix');
        const mainNumber = slide.dataset.wsMainNumber || null;

        // Walk every reveal step and keep the one with the worst overflow.
        // Custom deck CSS can toggle layout-affecting classes via data-class,
        // so layout isn't guaranteed stable across steps — always check each.
        let worst = null;
        for (let step = 0; step <= maxStep; step++) {
            WS.goToSlide(idx, step);
            const m = measureSlide(slide);
            const score = m.vOverflow + m.hOverflow;
            if (worst === null || score > (worst.vOverflow + worst.hOverflow)) {
                worst = m;
                worst.step = step;
            }
        }

        results.push({
            index: idx,
            maxStep: maxStep,
            step: worst.step,
            title: title,
            isAppendix: isAppendix,
            mainNumber: mainNumber,
            vOverflow: worst.vOverflow,
            hOverflow: worst.hOverflow,
            maxBottom: worst.maxBottom,
            maxRight: worst.maxRight,
            items: worst.items,
        });
    }

    return results;
}
"""


def pick_culprits(items, max_items):
    """Select the most informative overflowing elements.

    Strategy: prefer leaf-ish elements (few/no children) with the worst
    overflow. Ancestors whose overflow is entirely explained by a reported
    descendant add noise — so we sort by overflow descending, then prefer
    childCount=0, and deduplicate by (tag, text) to avoid reporting the
    same leaf wrapped in multiple containers.
    """
    if not items:
        return []

    def item_key(item):
        if item["id"]:
            return (item["tag"], "id", item["id"])
        return (
            item["tag"],
            item["cls"],
            item["text"],
            item["chain"],
        )

    items_sorted = sorted(
        items,
        key=lambda x: (-(x["bOver"] + x["rOver"]), x["childCount"], len(x["chain"])),
    )
    seen_text = set()
    result = []
    # First pass: leaves (no children)
    for x in items_sorted:
        if x["childCount"] != 0:
            continue
        key = item_key(x)
        if key in seen_text:
            continue
        seen_text.add(key)
        result.append(x)
        if len(result) >= max_items:
            return result
    # Second pass: small containers if still short
    for x in items_sorted:
        if x["childCount"] == 0:
            continue
        key = item_key(x)
        if key in seen_text:
            continue
        seen_text.add(key)
        result.append(x)
        if len(result) >= max_items:
            break
    return result


def checked_overflow_results(results, include_appendix=False):
    """Return the slide set that will be reported, rejecting empty checks."""
    if not isinstance(results, list):
        raise ValueError("Invalid overflow check results")

    required_int_fields = {
        "index", "maxStep", "step", "vOverflow", "hOverflow", "maxBottom", "maxRight",
    }
    for expected_index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError("Invalid overflow check result")
        for field in required_int_fields:
            if type(result.get(field)) is not int:
                raise ValueError(f"Invalid overflow check result: missing {field}")
            if result[field] < 0:
                raise ValueError(f"Invalid overflow check result: negative {field}")
        if result["index"] != expected_index:
            raise ValueError("Invalid overflow check result: non-contiguous index")
        if result["step"] > result["maxStep"]:
            raise ValueError("Invalid overflow check result: step exceeds maxStep")
        expected_v_overflow = max(0, result["maxBottom"] - SLIDE_HEIGHT)
        expected_h_overflow = max(0, result["maxRight"] - SLIDE_WIDTH)
        if result["vOverflow"] != expected_v_overflow or result["hOverflow"] != expected_h_overflow:
            raise ValueError("Invalid overflow check result: inconsistent overflow measurements")
        if not isinstance(result.get("isAppendix"), bool):
            raise ValueError("Invalid overflow check result: missing isAppendix")
        if not isinstance(result.get("items"), list):
            raise ValueError("Invalid overflow check result: missing items")
        if not isinstance(result.get("title", ""), str):
            raise ValueError("Invalid overflow check result: invalid title")
        main_number = result.get("mainNumber")
        if main_number is not None and not isinstance(main_number, str):
            raise ValueError("Invalid overflow check result: invalid mainNumber")
        required_item_strings = {"tag", "cls", "id", "text", "chain"}
        required_item_ints = {"bottom", "right", "bOver", "rOver", "childCount"}
        for item in result["items"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid overflow check result: malformed item")
            for field in required_item_strings:
                if not isinstance(item.get(field), str):
                    raise ValueError(f"Invalid overflow check result: malformed item {field}")
            for field in required_item_ints:
                if type(item.get(field)) is not int:
                    raise ValueError(f"Invalid overflow check result: malformed item {field}")
                if item[field] < 0:
                    raise ValueError(f"Invalid overflow check result: negative item {field}")

    if not include_appendix:
        results = [r for r in results if not r.get("isAppendix")]
    if not results:
        raise ValueError("No slides were checked")
    return results


async def check_overflow(
    html_path,
    include_appendix=False,
    verbose=False,
    json_out=False,
    strict=False,
):
    from playwright.async_api import async_playwright

    html_path = check_deck_file(resolve_deck_path(html_path))

    root = find_webslides_root(html_path)
    httpd = start_server(root, 0)
    port = httpd.server_address[1]
    url = build_deck_url(html_path, root, port)

    results = []
    load_errors = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT},
                    device_scale_factor=1,
                )
                page_errors = []
                page.on("pageerror", lambda e: page_errors.append(e))

                await page.goto(url, wait_until="load")
                validate_browser_deck_structure(await page.evaluate(DECK_STRUCTURE_JS))
                raise_for_page_errors(page_errors)
                # WS.ready (or the fallback poll: tables injected or failed,
                # KaTeX loaded), then web fonts: measuring with fallback font
                # metrics flags overflow that a warm-cache rerun does not.
                await wait_for_deck_ready(page, page_errors)
                load_errors = await page.evaluate(LOAD_ERRORS_JS)

                await page.evaluate("WS.prepareForExport()")
                max_steps = [s["maxStep"] for s in await page.evaluate(SLIDE_INFO_JS)]
                # Single round-trip: JS loops through every slide × every reveal
                # step internally, returning the worst measurement per slide.
                results = await page.evaluate(MEASURE_JS, max_steps)
                raise_for_page_errors(page_errors)
            finally:
                await browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    try:
        results = checked_overflow_results(results, include_appendix=include_appendix)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    overflowing = [r for r in results if r["vOverflow"] > 0 or r["hOverflow"] > 0]

    if json_out:
        print(json.dumps(
            {
                "total": len(results),
                "overflowing": len(overflowing),
                "loadErrors": load_errors,
                "slides": results,
            },
            indent=2,
        ))
    else:
        print("\n=== WebSlides Overflow Check (mechanical) ===\n")
        print(f"Checked {len(results)} slide(s) at 1280×720.")
        if load_errors:
            # A missing table or SVG makes its slide look emptier than it is,
            # so the overflow result for that slide cannot be trusted.
            print(f"  ✗ {len(load_errors)} asset(s) failed to load; overflow was not measured on them:")
            for err in load_errors:
                print(f"      · {format_load_error(err)}")
            print()
        if not overflowing:
            print("  ✓ All slides fit within bounds.\n")
        else:
            print(f"  ✗ {len(overflowing)} slide(s) overflow the slide box:\n")
            for r in overflowing:
                label = (
                    "Appendix"
                    if r.get("isAppendix")
                    else f"Slide {r.get('mainNumber') or (r['index'] + 1)}"
                )
                title = r["title"] or "(untitled)"
                step_info = f"(step {r['step']} of {r['maxStep']})" if r["maxStep"] > 0 else ""
                vo, ho = r["vOverflow"], r["hOverflow"]
                dims = []
                if vo > 0:
                    dims.append(
                        f"vertical +{vo}px (content reaches {r['maxBottom']}px / 720)"
                    )
                if ho > 0:
                    dims.append(
                        f"horizontal +{ho}px (content reaches {r['maxRight']}px / 1280)"
                    )
                print(f"  ⚠ {label}: \"{title}\" {step_info}".rstrip())
                print(f"      overflow: {'; '.join(dims)}")

                culprits = pick_culprits(r["items"], max_items=(10 if verbose else 3))
                for c in culprits:
                    sel = f"<{c['tag']}"
                    if c["id"]:
                        sel += f"#{c['id']}"
                    if c["cls"]:
                        classes = ".".join(c["cls"].split()[:2])
                        sel += f".{classes}"
                    sel += ">"
                    over_bits = []
                    if c["bOver"] > 0:
                        over_bits.append(f"bottom+{c['bOver']}px")
                    if c["rOver"] > 0:
                        over_bits.append(f"right+{c['rOver']}px")
                    over_str = ", ".join(over_bits)
                    text = c["text"] or "(no text)"
                    print(f"        · {sel} {over_str} — \"{text}\"")
                    if verbose:
                        print(f"            chain: {c['chain']}")
                print()

    # Failed loads always fail: the slides they sit on were not really checked.
    if load_errors or (strict and overflowing):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description="Mechanically check a WebSlides deck for content overflow."
    )
    ap.add_argument("html", help="Path to deck index.html")
    ap.add_argument(
        "--all",
        dest="include_appendix",
        action="store_true",
        help="Include appendix slides in the report",
    )
    ap.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="List up to 10 culprit elements per slide with ancestor chains",
    )
    ap.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any slide overflows (default: exit 0 unless a table or SVG failed to load)",
    )
    args = ap.parse_args()

    try:
        asyncio.run(
            check_overflow(
                args.html,
                include_appendix=args.include_appendix,
                verbose=args.verbose,
                json_out=args.json_out,
                strict=args.strict,
            )
        )
    except (OSError, RuntimeError, ValueError, ImportError, PlaywrightError, PlaywrightTimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
