"""Full-page PNGs of the static site for visual review (forge reviewers, and you).

Usage: `make screenshots`. Output: .forge/screenshots/<page>-<width>.png.
The pages are served from a copy of ui/ with tests/fixtures/site/ as ../data/ (the path they
use off github.io). The fixture is a cut-down copy of the data published on 2026-11-15 with
placeholder player names, so the pictures only change when the pages do.
Browser: CHROME_PATH (set in the forge runner image), else Playwright's own Chromium.
Icons and names still come from Data Dragon, so this needs network access.
"""

import functools
import os
import shutil
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".forge" / "screenshots"
WIDTHS = [375, 1280]


def open_champion(page):
    page.click("#table-kr tbody tr")
    page.wait_for_selector("#detail-view:not(.hidden)")


def open_champion_tooltip(page):
    open_champion(page)
    page.hover("#detail-items > *")


# name -> (path, steps to run after load)
PAGES = {
    "home": ("index.html", None),
    "champion": ("index.html", open_champion),
    "champion-tooltip": ("index.html", open_champion_tooltip),
    "history": ("history.html", None),
}


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(ROOT / "ui", Path(tmp) / "ui")
        shutil.copytree(ROOT / "tests" / "fixtures" / "site", Path(tmp) / "data")
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=tmp))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/ui/"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
                for name, (path, steps) in PAGES.items():
                    for width in WIDTHS:
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        page.goto(base + path, wait_until="networkidle", timeout=30_000)
                        if steps:
                            steps(page)
                            page.wait_for_load_state("networkidle")
                        page.screenshot(path=OUT / f"{name}-{width}.png", full_page=True)
                        page.close()
                        print(f"{name}-{width}.png")
                browser.close()
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
