"""Render the manual to PDF with headless Chrome.

The page carries a print stylesheet: the sticky rail turns into a
three-column table of contents on page 1, every section starts on a fresh
sheet, and tables shrink to the A4 text column instead of overflowing.

Usage:
    python docs/render_pdf.py
"""

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
HTML = DOCS / "nimat-manual.html"
PDF = DOCS / "nimat-manual.pdf"

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str:
    for candidate in CHROME_CANDIDATES:
        if pathlib.Path(candidate).exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit(
        "No Chrome or Edge found. Install one, or open "
        "docs/nimat-manual.html and print it to PDF by hand."
    )


def main() -> None:
    browser = find_browser()
    if PDF.exists():
        PDF.unlink()

    result = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={PDF}",
            HTML.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if not PDF.exists():
        sys.stderr.write(result.stderr[-2000:])
        raise SystemExit("PDF was not produced")

    print(f"wrote {PDF} ({PDF.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
