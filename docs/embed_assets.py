"""Embed the project's own fonts and the Nimat logo into the manual as
data URIs, so the published page renders identically everywhere and does
not depend on an external font host."""

import base64
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "nimat-manual.html"
FONTS = ROOT / "app" / "fonts"

MIME = "font/ttf"
PNG = "image/png"


def data_uri(path: pathlib.Path, mime: str) -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def main() -> None:
    html = MANUAL.read_text(encoding="utf-8")

    replacements = {
        "__FONT_REG__": data_uri(FONTS / "IRANSansWeb.ttf", MIME),
        "__FONT_MED__": data_uri(FONTS / "IRANSansWeb_Medium.ttf", MIME),
        "__FONT_BLK__": data_uri(FONTS / "IRANSansWeb_Black.ttf", MIME),
        "__FONT_MONO__": data_uri(FONTS / "IRANSansWeb.ttf", MIME),
        "__LOGO__": data_uri(FONTS / "nimat_logo.png", PNG),
    }

    for token, value in replacements.items():
        if token not in html:
            raise SystemExit(f"missing token in manual: {token}")
        html = html.replace(token, value)

    leftover = re.findall(r"__[A-Z_]+__", html)
    if leftover:
        raise SystemExit("unresolved tokens: " + ", ".join(sorted(set(leftover))))

    MANUAL.write_text(html, encoding="utf-8")
    size = MANUAL.stat().st_size
    print(f"embedded fonts and logo -> {MANUAL} ({size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
