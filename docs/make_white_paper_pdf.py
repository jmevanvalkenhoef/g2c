# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown==3.7"]
# ///
"""Builds WHITE_PAPER.pdf from WHITE_PAPER.md (A4, images from img/).

    uv run docs/make_white_paper_pdf.py

Markdown becomes HTML with the stylesheet below, and headless Chromium prints
it to PDF. Needs `chromium` (or set CHROMIUM to another Chrome binary).
"""

import os
import subprocess
import tempfile
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
SOURCE = DOCS / "WHITE_PAPER.md"
TARGET = DOCS / "WHITE_PAPER.pdf"

CSS = """
@page { size: A4; margin: 22mm 20mm; }
body { font: 10.5pt/1.5 Georgia, "Liberation Serif", serif; color: #1b1916; }
h1 { font-size: 20pt; margin: 0 0 2pt; }
h1 + p { color: #7d7568; margin-top: 0; }
h2 { font-size: 13pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1px solid #e2d6c6; break-after: avoid; }
p { margin: 0 0 6pt; }
li { margin: 0 0 2pt; }
ul { padding-left: 16pt; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0 10pt; font-size: 9pt; break-inside: avoid; }
th { text-align: left; background: #f1ebe2; }
th, td { padding: 5pt 8pt; border-bottom: 1px solid #e2d6c6; vertical-align: top; }
img { display: block; max-width: 100%; max-height: 92mm; margin: 8pt auto; break-inside: avoid; }
code { font-size: 9pt; }
"""


def main() -> None:
    body = markdown.markdown(SOURCE.read_text(), extensions=["tables"])
    title = SOURCE.read_text().splitlines()[0].lstrip("# ").split(":")[0] + " white paper"
    page = f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>{CSS}</style></head><body>{body}</body></html>'
    # The HTML goes next to img/ so the images resolve; it is removed afterwards.
    with tempfile.NamedTemporaryFile("w", suffix=".html", dir=DOCS, delete=False) as f:
        f.write(page)
    try:
        subprocess.run(
            [os.environ.get("CHROMIUM", "chromium"), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={TARGET}", Path(f.name).as_uri()],
            check=True, capture_output=True, timeout=120,
        )
    finally:
        os.unlink(f.name)
    print(f"wrote {TARGET}")


if __name__ == "__main__":
    main()
