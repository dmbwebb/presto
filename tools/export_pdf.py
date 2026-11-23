#!/usr/bin/env python3
"""Export a WebSlides deck to PDF using Playwright.

Usage:
    python3 tools/export_pdf.py path/to/index.html
    python3 tools/export_pdf.py path/to/index.html --output talk.pdf
    python3 tools/export_pdf.py path/to/index.html --flat

By default, each reveal step becomes a separate PDF page (so a slide with
3 bullet reveals produces 4 pages: step 0 through step 3).

Use --flat for one page per slide with all steps visible (old behavior), except
ranged reveals ("1-4") that end before the slide's last step.

Requires: pip install playwright pillow pypdf && playwright install chromium
(``python3 tools/export_pdf.py`` re-runs itself under ~/.venvs/webslides-export
when the current interpreter lacks them.)
"""

import argparse
import asyncio
import math
import subprocess
import sys
import uuid
from io import BytesIO
from pathlib import Path

if __name__ == '__main__':
    from _common import ensure_tool_python

    ensure_tool_python(('playwright', 'pypdf', 'PIL'))

from _common import (  # noqa: E402
    DECK_STRUCTURE_JS,
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
    build_deck_url,
    engine_js,
    find_webslides_root,
    raise_for_page_errors,
    resolve_deck_path,
    start_server,
    strict_int,
    wait_for_deck_ready,
)
from _common import validate_browser_deck_structure as _validate_deck_structure  # noqa: E402

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    PlaywrightError = PlaywrightTimeoutError = RuntimeError


def trash_path(path):
    """Move an existing generated file to Trash before writing a replacement."""
    if not path.exists():
        return
    try:
        subprocess.run(['trash', str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f'Could not move existing output to Trash: {path}') from exc


def prepare_output_path(output_path):
    """Ensure the PDF output parent directory exists before atomic publish."""
    output_path = Path(output_path).expanduser()
    if output_path.exists() and output_path.is_dir():
        raise ValueError(f'Output path is a directory, not a PDF file: {output_path}')
    if output_path.suffix.lower() != '.pdf':
        raise ValueError(f'Output path is not a PDF file: {output_path}')
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f'could not prepare output directory {output_path.parent}: {exc}') from exc
    return output_path


def watch_page_errors(page):
    """Collect uncaught browser errors so exports can fail loudly."""
    page_errors = []
    page.on('pageerror', lambda exc: page_errors.append(exc))
    return page_errors


def write_pdf_writer_atomically(writer, output_path, *, expected_pages=None):
    """Write a pypdf writer through a validated sibling temp file, then replace output."""
    output_path = Path(output_path).expanduser()
    tmp_path = output_path.with_name(f'.{output_path.name}.tmp-{uuid.uuid4().hex}')
    try:
        with tmp_path.open('wb') as f:
            writer.write(f)
        validate_pdf_output(tmp_path, expected_pages=expected_pages)
        tmp_path.replace(output_path)
    except Exception:
        if tmp_path.exists():
            try:
                trash_path(tmp_path)
            except RuntimeError:
                pass
        raise


def write_raster_pdf_atomically(output_path, screenshots):
    """Write screenshot bytes to a raster PDF through a validated temp file."""
    from PIL import Image

    if not screenshots:
        raise RuntimeError('No PDF pages were captured')

    output_path = Path(output_path).expanduser()
    tmp_path = output_path.with_name(f'.{output_path.name}.tmp-{uuid.uuid4().hex}')

    def open_img(data):
        img = Image.open(BytesIO(data))
        return img.convert('RGB') if img.mode == 'RGBA' else img

    try:
        first = open_img(screenshots[0])
        first.save(
            str(tmp_path),
            'PDF',
            save_all=True,
            append_images=(open_img(d) for d in screenshots[1:]),
            resolution=144.0,
        )
        validate_pdf_output(tmp_path, expected_pages=len(screenshots))
        tmp_path.replace(output_path)
    except Exception:
        if tmp_path.exists():
            try:
                trash_path(tmp_path)
            except RuntimeError:
                pass
        raise


async def write_browser_pdf_atomically(page, output_path, *, expected_pages, page_errors=None):
    """Ask Playwright to write a PDF to a temp file, validate it, then publish."""
    output_path = Path(output_path).expanduser()
    tmp_path = output_path.with_name(f'.{output_path.name}.tmp-{uuid.uuid4().hex}')
    try:
        await page.pdf(
            path=str(tmp_path),
            width=f'{SLIDE_WIDTH}px',
            height=f'{SLIDE_HEIGHT}px',
            print_background=True,
            margin={'top': '0', 'right': '0', 'bottom': '0', 'left': '0'},
        )
        validate_pdf_output(tmp_path, expected_pages=expected_pages)
        raise_for_page_errors(page_errors or [])
        tmp_path.replace(output_path)
    except Exception:
        if tmp_path.exists():
            try:
                trash_path(tmp_path)
            except RuntimeError:
                pass
        raise


def validate_export_info(slide_info):
    """Validate browser export metadata and return a flat (slide, step) plan."""
    if not isinstance(slide_info, list) or not slide_info:
        raise ValueError('No exportable slides found')

    page_plan = []
    seen_pages = set()
    for expected_idx, entry in enumerate(slide_info):
        if not isinstance(entry, dict):
            raise ValueError('Invalid export slide info')
        idx = entry.get('index')
        max_step = entry.get('maxStep')
        if type(idx) is not int or type(max_step) is not int or idx < 0 or max_step < 0:
            raise ValueError('Invalid export slide info')
        if idx != expected_idx:
            raise ValueError('Invalid export slide info')
        for step in range(max_step + 1):
            key = (idx, step)
            if key in seen_pages:
                raise ValueError('Duplicate export page info')
            seen_pages.add(key)
            page_plan.append(key)

    if not page_plan:
        raise ValueError('No export pages found')
    return page_plan


def validate_flat_slide_count(total):
    """Validate flat-export slide count from the browser."""
    if type(total) is not int or total <= 0:
        raise ValueError('No exportable slides found')
    return total


def validate_browser_deck_structure(info):
    """Validate browser-observed deck structure before waiting on runtime state."""
    return _validate_deck_structure(info, empty_message='No exportable slides found')


def strict_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def validate_export_links(links):
    """Validate per-page link metadata returned by the browser."""
    if not isinstance(links, list):
        raise ValueError('Invalid export link metadata')
    for link in links:
        if not isinstance(link, dict):
            raise ValueError('Invalid export link metadata')
        if strict_int(link.get('targetSlideIndex'), min_value=0) is None or strict_int(link.get('targetStep'), min_value=0) is None:
            raise ValueError('Invalid export link metadata')
        for key in ('x', 'y', 'w', 'h'):
            if strict_finite_number(link.get(key)) is None:
                raise ValueError('Invalid export link metadata')
        if strict_finite_number(link.get('w')) <= 0 or strict_finite_number(link.get('h')) <= 0:
            raise ValueError('Invalid export link metadata')
    return links


def validate_export_link_targets(page_links, page_map):
    """Validate browser link metadata points to pages in the export plan."""
    valid_source_pages = set(page_map.values())
    if set(range(len(page_links))) != valid_source_pages:
        raise ValueError('Invalid export link metadata')
    for page_idx, links in enumerate(page_links):
        if page_idx not in valid_source_pages or not isinstance(links, list):
            raise ValueError('Invalid export link metadata')
        for link in links:
            target = page_map.get((link['targetSlideIndex'], link['targetStep']))
            if target is None:
                raise ValueError('Export link target is not in the export page plan')
    return page_links


def validate_pdf_output(output_path, expected_pages=None):
    """Validate that an exported PDF exists, is non-empty, and has expected pages."""
    output_path = Path(output_path).expanduser()
    if not output_path.exists():
        raise RuntimeError(f'PDF output was not created: {output_path}')
    if output_path.stat().st_size <= 0:
        raise RuntimeError(f'PDF output is empty: {output_path}')
    if expected_pages is None:
        return

    from pypdf import PdfReader

    reader = PdfReader(str(output_path))
    actual_pages = len(reader.pages)
    if actual_pages != expected_pages:
        raise RuntimeError(
            f'PDF page count mismatch: expected {expected_pages}, got {actual_pages}'
        )


def normalized_pdf_links(page_links, page_map):
    """Return validated link annotations as (page_idx, target, link) tuples."""
    normalized = []
    valid_pages = set(page_map.values())
    for page_idx, links in enumerate(page_links):
        if page_idx not in valid_pages:
            continue
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            target_slide = strict_int(link.get('targetSlideIndex'))
            target_step = strict_int(link.get('targetStep'))
            if target_slide is None or target_step is None:
                continue
            target = page_map.get((target_slide, target_step))
            if target is None or target == page_idx:
                continue

            rect = {}
            valid_rect = True
            for key in ('x', 'y', 'w', 'h'):
                value = strict_finite_number(link.get(key))
                if value is None:
                    valid_rect = False
                    break
                rect[key] = value
            if not valid_rect or rect['w'] <= 0 or rect['h'] <= 0:
                continue

            x1 = max(0.0, rect['x'])
            y1 = max(0.0, rect['y'])
            x2 = min(float(SLIDE_WIDTH), rect['x'] + rect['w'])
            y2 = min(float(SLIDE_HEIGHT), rect['y'] + rect['h'])
            if x2 <= x1 or y2 <= y1:
                continue
            rect = {
                'x': x1,
                'y': y1,
                'w': x2 - x1,
                'h': y2 - y1,
            }

            normalized.append((page_idx, target, rect))
    return normalized


async def open_ready_deck(page, url, page_errors):
    """Load the deck, check its structure, and wait for the engine to be ready.

    Readiness is WS.ready when the engine has it (else the shared fallback
    poll), then web fonts and the deck's images.
    """
    await page.goto(url, wait_until='load')
    validate_browser_deck_structure(await page.evaluate(DECK_STRUCTURE_JS))
    raise_for_page_errors(page_errors)
    await wait_for_deck_ready(page, page_errors)


async def refit_tables_for_export(page):
    """Recalculate table overlays and wait for layout to paint."""
    await page.evaluate("""() => {
        if (window.WS && typeof WS.refitTables === "function") {
            return WS.refitTables().then(() => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            }));
        }
    }""")


async def export_pdf_steps(url, output_path):
    """Step-by-step export: one PDF page per reveal state."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(
                viewport={'width': SLIDE_WIDTH, 'height': SLIDE_HEIGHT},
                device_scale_factor=2,
            )
            page_errors = watch_page_errors(page)

            await open_ready_deck(page, url, page_errors)

            await page.evaluate('WS.prepareForExport()')
            slide_info = await page.evaluate('WS.getExportInfo()')
            page_plan = validate_export_info(slide_info)

            total_pages = len(page_plan)
            print(f'  Found {len(slide_info)} slides, {total_pages} pages (with reveal steps)')

            # Build (slideIndex, step) → pageNumber mapping
            page_map = {}
            for page_num, (slide_index, step) in enumerate(page_plan):
                page_map[(slide_index, step)] = page_num

            # Capture screenshots and link info per page
            screenshots = []
            page_links = []
            for page_num, (slide_index, step) in enumerate(page_plan, start=1):
                await page.evaluate(
                    """({slideIndex, step}) => {
                        function verifyExportNavigation(state) {
                            if (!state || state.index !== slideIndex || state.step !== step) {
                                throw new Error(
                                    `Export navigation mismatch: requested ${slideIndex}/${step}, got ` +
                                    `${state ? state.index : 'unknown'}/${state ? state.step : 'unknown'}`
                                );
                            }
                        }
                        if (!window.WS || typeof WS.goToSlide !== 'function' || typeof WS.getState !== 'function') {
                            throw new Error('WebSlides export API is unavailable');
                        }
                        WS.goToSlide(slideIndex, step);
                        verifyExportNavigation(WS.getState());
                    }""",
                    {'slideIndex': slide_index, 'step': step},
                )
                await refit_tables_for_export(page)
                await page.wait_for_timeout(50)
                screenshots.append(await page.screenshot())
                links = await page.evaluate('WS.getExportLinks()')
                page_links.append(validate_export_links(links))
                if page_num % 20 == 0:
                    print(f'  Captured {page_num}/{total_pages} pages...')
            raise_for_page_errors(page_errors)
        finally:
            await browser.close()

        if len(screenshots) != total_pages:
            raise RuntimeError(f'Captured {len(screenshots)} of {total_pages} PDF pages')

        write_stepped_pdf_atomically(output_path, screenshots, page_links, page_map)
        print(f'  Exported {total_pages} pages to {output_path}')


def add_pdf_links(pdf_path, page_links, page_map):
    """Add internal link annotations to a PDF using pypdf."""
    normalized_links = normalized_pdf_links(page_links, page_map)
    if not normalized_links:
        return

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject, DictionaryObject, FloatObject,
        NameObject, NumberObject,
    )

    reader = PdfReader(str(pdf_path))
    expected_pages = len(reader.pages)
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)

    link_count = 0
    for page_idx, target, link in normalized_links:
        pdf_page = writer.pages[page_idx]

        # Convert from screen coords (top-left origin) to PDF (bottom-left)
        x1 = link['x']
        y1 = SLIDE_HEIGHT - (link['y'] + link['h'])
        x2 = link['x'] + link['w']
        y2 = SLIDE_HEIGHT - link['y']

        annot = DictionaryObject()
        annot[NameObject('/Type')] = NameObject('/Annot')
        annot[NameObject('/Subtype')] = NameObject('/Link')
        annot[NameObject('/Rect')] = ArrayObject([
            FloatObject(x1), FloatObject(y1),
            FloatObject(x2), FloatObject(y2),
        ])
        annot[NameObject('/Border')] = ArrayObject([
            NumberObject(0), NumberObject(0), NumberObject(0),
        ])
        annot[NameObject('/Dest')] = ArrayObject([
            writer.pages[target].indirect_reference,
            NameObject('/Fit'),
        ])

        if '/Annots' not in pdf_page:
            pdf_page[NameObject('/Annots')] = ArrayObject()
        pdf_page[NameObject('/Annots')].append(annot)
        link_count += 1

    if link_count > 0:
        write_pdf_writer_atomically(writer, pdf_path, expected_pages=expected_pages)
        print(f'  Added {link_count} clickable links')


def write_stepped_pdf_atomically(output_path, screenshots, page_links, page_map):
    """Write stepped PDF pages and link annotations before replacing the output."""
    output_path = Path(output_path).expanduser()
    tmp_path = output_path.with_name(f'.{output_path.name}.tmp-{uuid.uuid4().hex}')
    try:
        write_raster_pdf_atomically(tmp_path, screenshots)
        validate_export_link_targets(page_links, page_map)
        add_pdf_links(tmp_path, page_links, page_map)
        validate_pdf_output(tmp_path, expected_pages=len(screenshots))
        tmp_path.replace(output_path)
    except Exception:
        if tmp_path.exists():
            try:
                trash_path(tmp_path)
            except RuntimeError:
                pass
        raise


async def export_pdf_flat(url, output_path):
    """Flat export: one page per slide, all steps visible except ranged reveals that end early."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(
                viewport={'width': SLIDE_WIDTH, 'height': SLIDE_HEIGHT}
            )
            page_errors = watch_page_errors(page)

            await open_ready_deck(page, url, page_errors)

            total = validate_flat_slide_count(await page.evaluate('() => WS.slides.length'))
            print(f'  Found {total} slides (flat mode)')

            # Cancels the slide fade-in still running from init: transitions
            # outrank the print CSS's opacity: 1 !important.
            await page.evaluate('WS.prepareForExport()')
            await page.evaluate(FLAT_LAYOUT_JS)
            await page.evaluate(FLAT_REVEAL_JS)
            await refit_tables_for_export(page)
            # The refit re-applies the current slide's step-0 reveal state.
            await page.evaluate(FLAT_REVEAL_JS)

            await write_browser_pdf_atomically(
                page,
                output_path,
                expected_pages=total,
                page_errors=page_errors,
            )
            raise_for_page_errors(page_errors)
        finally:
            await browser.close()
        print(f'  Exported {total} slides to {output_path}')


# Flat page layout: every engine slide stacked as one print page.
FLAT_LAYOUT_JS = r'''() => {
    const deck = document.querySelector(".ws-deck");
    deck.style.position = "static";
    deck.style.width = "auto";
    deck.style.height = "auto";
    deck.style.overflow = "visible";

    document.querySelectorAll(".ws-deck .slide").forEach(s => {
        if (s.parentElement !== deck) {
            s.style.display = "none";
        }
    });

    document.querySelectorAll(".ws-deck > .slide").forEach(s => {
        s.classList.add("ws-active");
        s.style.position = "relative";
        s.style.top = "auto";
        s.style.left = "auto";
        s.style.transform = "none";
        s.style.opacity = "1";
        s.style.pointerEvents = "auto";
        s.style.pageBreakAfter = "always";
        s.style.pageBreakInside = "avoid";
    });

    var footer = document.getElementById("ws-footer");
    if (footer) footer.style.display = "none";
    var progress = document.getElementById("ws-progress");
    if (progress) progress.style.display = "none";

    document.body.style.overflow = "visible";
    document.body.style.height = "auto";
    document.body.style.background = "white";
}'''

# Flat reveal state: everything shown, except ranged reveals that end before
# the slide's last step. Step counts and range parsing come from the engine
# (wsMaxStep / wsElRange / wsRange, see _common.ENGINE_HELPERS_JS).
FLAT_REVEAL_JS = engine_js(r'''
    // Ranged reveals ("1-4") that end before the slide's last step are
    // swapped out live (often stacked in the same grid cell), so the
    // flat page leaves them hidden instead of overprinting them.
    const engineSlides = wsSlides();
    document.querySelectorAll("[data-step]").forEach(el => {
        const range = wsElRange(el, "step");
        const slideIndex = engineSlides.indexOf(el.closest(".ws-deck > section.slide"));
        const lastStep = slideIndex >= 0 ? wsMaxStep(slideIndex) : 0;
        if (range && range.end !== null && range.end < lastStep) {
            // print CSS forces [data-step] opacity to 1, so hide via visibility
            el.classList.remove("ws-visible");
            el.style.setProperty("visibility", "hidden", "important");
            return;
        }
        el.classList.add("ws-visible");
        el.style.opacity = "1";
    });

    document.querySelectorAll("[data-highlight]").forEach(el => {
        el.classList.add("ws-highlighted");
    });

    document.querySelectorAll(".ws-dimmed").forEach(el => {
        el.classList.remove("ws-dimmed");
    });
    document.querySelectorAll("[data-dim]").forEach(el => {
        el.classList.remove("ws-dimmed");
    });
    // Fades (data-fade-after, data-focus) are undone like dims
    document.querySelectorAll(".ws-faded").forEach(el => {
        el.classList.remove("ws-faded");
    });

    document.querySelectorAll("[data-class]").forEach(el => {
        const raw = el.dataset.class || "";
        const sep = raw.lastIndexOf(":");
        if (sep <= 0) return;
        const className = raw.slice(0, sep).trim();
        const range = raw.slice(sep + 1).trim();
        if (isValidClassToken(className) && wsRange(range)) {
            el.classList.add(className);
        }
    });

    function isValidClassToken(className) {
        return typeof className === "string" && className !== "" && !/[\t\n\f\r ]/.test(className);
    }
''')


async def export_pdf(html_path, output_path=None, flat=False):
    html_path = resolve_deck_path(html_path)
    if not html_path.exists():
        raise ValueError(f'{html_path} does not exist')
    if html_path.is_dir():
        raise ValueError(f'{html_path} is a directory, not an HTML file')
    if not html_path.is_file():
        raise ValueError(f'{html_path} is not a file')
    if html_path.suffix.lower() != '.html':
        raise ValueError(f'{html_path} is not an HTML file')

    if output_path is None:
        output_path = prepare_output_path(html_path.parent / 'slides.pdf')
    else:
        if Path(output_path).expanduser().resolve() == html_path:
            raise ValueError(f'Output path must not be the input deck: {html_path}')
        output_path = prepare_output_path(output_path)

    print(f'Exporting {html_path} ...')

    # Serve via HTTP to allow fetch() for tables and SVG inlining
    root = find_webslides_root(html_path)
    httpd = start_server(root, 0)
    port = httpd.server_address[1]
    url = build_deck_url(html_path, root, port)

    try:
        if flat:
            await export_pdf_flat(url, output_path)
        else:
            await export_pdf_steps(url, output_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a WebSlides deck to PDF using Playwright.",
    )
    parser.add_argument("deck", help="Path to a deck index.html or deck directory.")
    parser.add_argument("--output", help="Output PDF path. Defaults to slides.pdf beside the deck.")
    parser.add_argument("--flat", action="store_true", help="Export one page per slide with all steps visible (ranged reveals that end early stay hidden).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(export_pdf(args.deck, args.output, flat=args.flat))
    except (OSError, RuntimeError, ValueError, ImportError, PlaywrightError, PlaywrightTimeoutError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == '__main__':
    main()
