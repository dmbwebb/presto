#!/usr/bin/env python3
"""Convert PDF figures to SVG using pdftocairo, with transparent background by default.

Usage:
    python3 tools/pdf2svg.py input.pdf                    # -> input.svg (transparent)
    python3 tools/pdf2svg.py input.pdf output.svg         # explicit output
    python3 tools/pdf2svg.py input.pdf --page 2           # specific page
    python3 tools/pdf2svg.py figures/*.pdf --outdir svgs/ # batch mode
    python3 tools/pdf2svg.py figures/*.svg                # post-process existing SVGs in place
    python3 tools/pdf2svg.py input.pdf --keep-white       # opt out of background stripping

Transparent mode (default) strips pdftocairo's full-canvas white <rect> backgrounds
and zeroes fill-opacity on inner white-fill paths (ggplot panel/axis backgrounds).
"""

import argparse
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


WHITE_FILL = r'fill="rgb\(100%, ?100%, ?100%\)"'

# White <rect> elements emitted as the page-canvas background. Inner ggplot panel
# backgrounds come through as <path>, not <rect>, so deleting white-fill rects is
# safe in practice.
RE_WHITE_RECT = re.compile(
    r'<rect\b[^/<>]*?' + WHITE_FILL + r'[^/<>]*?(?:/>\s*\n?|>\s*</rect>\s*\n?)',
    re.IGNORECASE,
)

# fill="rgb(100%,...)" followed by fill-opacity="..." — the common pdftocairo ordering.
RE_FILL_THEN_OPACITY = re.compile(
    r'(' + WHITE_FILL + r')(\s+)fill-opacity="[^"]*"',
    re.IGNORECASE,
)

# fill-opacity="..." followed by fill="rgb(100%,...)" — handle the reverse ordering too.
RE_OPACITY_THEN_FILL = re.compile(
    r'fill-opacity="[^"]*"(\s+)(' + WHITE_FILL + r')',
    re.IGNORECASE,
)


def make_transparent(svg_text):
    """Strip white background from an SVG string. Returns (new_text, n_rects_removed, n_paths_zeroed)."""
    new_text, n_rects = RE_WHITE_RECT.subn('', svg_text)

    n_paths = 0
    new_text, k = RE_FILL_THEN_OPACITY.subn(r'\1\2fill-opacity="0"', new_text)
    n_paths += k
    new_text, k = RE_OPACITY_THEN_FILL.subn(r'fill-opacity="0"\1\2', new_text)
    n_paths += k

    return new_text, n_rects, n_paths


def write_text_atomically(path, text):
    """Write text through a same-directory temp file, then atomically replace path."""
    path = Path(path).expanduser()
    tmp_path = path.with_name(f'.{path.name}.tmp-{uuid.uuid4().hex}')
    try:
        tmp_path.write_text(text, encoding='utf-8')
        tmp_path.replace(path)
    except OSError:
        if tmp_path.exists():
            try:
                trash_path(tmp_path)
            except RuntimeError:
                pass
        raise


def transparentize_in_place(svg_path):
    """Apply make_transparent() to an existing SVG file, overwriting it."""
    svg_path = Path(svg_path).expanduser()
    if not svg_path.exists():
        print(f'  ERROR: {svg_path} does not exist')
        return False
    if svg_path.is_dir():
        print(f'  ERROR: {svg_path} is a directory, not an SVG file')
        return False
    try:
        text = svg_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        print(f'  ERROR: could not read {svg_path}: {exc}')
        return False
    if not text.strip() or not re.search(r'<svg\b', text, re.IGNORECASE):
        print(f'  ERROR: {svg_path} is not a valid SVG')
        return False
    new_text, n_rects, n_paths = make_transparent(text)
    if n_rects == 0 and n_paths == 0:
        print(f'  {svg_path}: already transparent')
        return True
    try:
        write_text_atomically(svg_path, new_text)
    except OSError as exc:
        print(f'  ERROR: could not write {svg_path}: {exc}')
        return False
    print(f'  {svg_path}: removed {n_rects} bg rect(s), zeroed {n_paths} white-fill path(s)')
    return True


def trash_path(path):
    """Move an existing generated output to Trash before replacing it."""
    path = Path(path).expanduser()
    if not path.exists():
        return
    try:
        subprocess.run(['trash', str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f'Could not move existing output to Trash: {path}') from exc


def validate_svg_output(output_svg):
    """Return SVG text after checking the converter produced plausible SVG."""
    output_svg = Path(output_svg).expanduser()
    if not output_svg.exists():
        raise ValueError(f'pdftocairo did not create {output_svg}')
    try:
        text = output_svg.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f'could not read {output_svg}: {exc}') from exc
    if not text.strip() or not re.search(r'<svg\b', text, re.IGNORECASE):
        raise ValueError(f'{output_svg} is not a valid SVG')
    return text


def pdf_to_svg(input_pdf, output_svg=None, page=None, transparent=True):
    """Convert a PDF to SVG via pdftocairo, optionally stripping the white background."""
    input_path = Path(input_pdf).expanduser()
    if not input_path.exists():
        print(f'  ERROR: {input_path} does not exist')
        return None
    if input_path.is_dir():
        print(f'  ERROR: {input_path} is a directory, not a PDF file')
        return None

    if output_svg is None:
        output_svg = input_path.with_suffix('.svg')
    else:
        output_svg = Path(output_svg).expanduser()

    if output_svg.resolve() == input_path.resolve():
        print(f'  ERROR: Output path must not be the input PDF: {input_path}')
        return None

    if output_svg.suffix.lower() != '.svg':
        print(f'  ERROR: Output path is not an SVG file: {output_svg}')
        return None

    if output_svg.exists() and output_svg.is_dir():
        print(f'  ERROR: Output path is a directory, not an SVG file: {output_svg}')
        return None

    if page is not None:
        try:
            page = int(page)
        except (TypeError, ValueError):
            print(f'  ERROR: Page must be an integer >= 1: {page!r}')
            return None
        if page < 1:
            print(f'  ERROR: Page must be >= 1: {page}')
            return None

    if shutil.which('pdftocairo') is None:
        print('  ERROR: pdftocairo not found. Install with: brew install poppler')
        sys.exit(1)

    try:
        output_svg.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f'  ERROR: could not prepare output directory {output_svg.parent}: {exc}')
        return None
    temp_svg = output_svg.with_name(f'.{output_svg.name}.tmp-{uuid.uuid4().hex}{output_svg.suffix}')

    cmd = ['pdftocairo', '-svg']
    if page is not None:
        cmd += ['-f', str(page), '-l', str(page)]
    cmd += [str(input_path), str(temp_svg)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if temp_svg.exists():
            try:
                trash_path(temp_svg)
            except RuntimeError as trash_exc:
                print(f'  ERROR: {trash_exc}')
        print(f'  ERROR converting {input_path}: {e.stderr}')
        return None
    except FileNotFoundError:
        if temp_svg.exists():
            try:
                trash_path(temp_svg)
            except RuntimeError as trash_exc:
                print(f'  ERROR: {trash_exc}')
        print('  ERROR: pdftocairo not found. Install with: brew install poppler')
        sys.exit(1)
    except OSError as exc:
        if temp_svg.exists():
            try:
                trash_path(temp_svg)
            except RuntimeError as trash_exc:
                print(f'  ERROR: {trash_exc}')
        print(f'  ERROR converting {input_path}: could not run pdftocairo: {exc}')
        return None

    try:
        text = validate_svg_output(temp_svg)
    except ValueError as exc:
        print(f'  ERROR converting {input_path}: {exc}')
        try:
            trash_path(temp_svg)
        except RuntimeError as trash_exc:
            print(f'  ERROR: {trash_exc}')
        return None

    if transparent:
        new_text, n_rects, n_paths = make_transparent(text)
        if n_rects or n_paths:
            try:
                write_text_atomically(temp_svg, new_text)
            except OSError as exc:
                try:
                    trash_path(temp_svg)
                except RuntimeError as trash_exc:
                    print(f'  ERROR: {trash_exc}')
                print(f'  ERROR converting {input_path}: could not write {temp_svg}: {exc}')
                return None
        try:
            temp_svg.replace(output_svg)
        except OSError as exc:
            try:
                trash_path(temp_svg)
            except RuntimeError as trash_exc:
                print(f'  ERROR: {trash_exc}')
            print(f'  ERROR converting {input_path}: could not publish {output_svg}: {exc}')
            return None
        print(f'  {input_path} -> {output_svg}: removed {n_rects} bg rect(s), zeroed {n_paths} white-fill path(s)')
    else:
        try:
            temp_svg.replace(output_svg)
        except OSError as exc:
            try:
                trash_path(temp_svg)
            except RuntimeError as trash_exc:
                print(f'  ERROR: {trash_exc}')
            print(f'  ERROR converting {input_path}: could not publish {output_svg}: {exc}')
            return None
        print(f'  {input_path} -> {output_svg}')

    return output_svg


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def has_suffix(path, suffix):
    return Path(path).suffix.lower() == suffix


def planned_batch_outputs(files, outdir):
    outputs = []
    for f in files:
        output = outdir / Path(f).with_suffix('.svg').name if outdir else Path(f).with_suffix('.svg')
        outputs.append((f, output.expanduser()))
    return outputs


def duplicate_output_issues(planned_outputs):
    seen = {}
    issues = []
    for source, output in planned_outputs:
        resolved = output.resolve()
        if resolved in seen:
            issues.append((output, seen[resolved], source))
        else:
            seen[resolved] = source
    return issues


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert PDF figures to SVG using pdftocairo.",
    )
    parser.add_argument("files", nargs="*", help="PDF files to convert or SVG files to transparentize.")
    parser.add_argument("--page", type=positive_int, help="One-based PDF page number to convert.")
    parser.add_argument("--outdir", type=Path, help="Output directory for batch conversion.")
    parser.add_argument(
        "--keep-white",
        "--no-transparent",
        dest="transparent",
        action="store_false",
        default=True,
        help="Keep white backgrounds instead of making the SVG transparent.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.files:
        print(__doc__)
        sys.exit(1)

    page = args.page
    outdir = args.outdir.expanduser() if args.outdir else None
    transparent = args.transparent
    files = args.files

    if not files:
        print('No input files specified.')
        sys.exit(1)

    if outdir and outdir.exists() and not outdir.is_dir():
        print(f'Output directory path is a file, not a directory: {outdir}')
        sys.exit(1)

    # If the only inputs are .svg files, post-process them in place.
    if all(has_suffix(f, '.svg') for f in files):
        if outdir:
            print('Cannot use --outdir with SVG inputs; SVG inputs are transparentized in place.')
            sys.exit(1)
        if page is not None:
            print('Cannot use --page with SVG inputs; --page only applies to PDF conversion.')
            sys.exit(1)
        if not transparent:
            print('Nothing to do: --keep-white with .svg inputs.')
            sys.exit(0)
        failed = False
        for f in files:
            if not transparentize_in_place(f):
                failed = True
        if failed:
            sys.exit(1)
        return

    # Single file with explicit output
    if len(files) == 2 and has_suffix(files[1], '.svg') and has_suffix(files[0], '.pdf'):
        if outdir:
            print('Cannot use --outdir with an explicit SVG output path.')
            sys.exit(1)
        if pdf_to_svg(files[0], files[1], page=page, transparent=transparent) is None:
            sys.exit(1)
        return

    non_pdf_inputs = [f for f in files if not has_suffix(f, '.pdf')]
    if non_pdf_inputs:
        print('Cannot mix PDF and SVG inputs. Pass only PDFs to convert, or only SVGs to transparentize.')
        for f in non_pdf_inputs:
            print(f'  ERROR: not a PDF input for batch conversion: {f}')
        sys.exit(1)

    # Batch mode
    planned_outputs = planned_batch_outputs(files, outdir)
    duplicate_outputs = duplicate_output_issues(planned_outputs)
    if duplicate_outputs:
        print('Duplicate output path(s) in batch conversion. Rename inputs or choose separate output directories.')
        for output, first_source, duplicate_source in duplicate_outputs:
            print(f'  ERROR: {first_source} and {duplicate_source} both write {output}')
        sys.exit(1)

    failed = False
    for f, planned_output in planned_outputs:
        out = planned_output if outdir else None
        if pdf_to_svg(f, out, page=page, transparent=transparent) is None:
            failed = True
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
