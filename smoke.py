#!/usr/bin/env python3
"""Load the built site in a real browser and check it actually works.

Every other suite tests something *about* the code. This one runs it. The
distinction earned its place: a refactor once changed a function's signature and
left one call site passing a stale variable, so the page threw a ReferenceError
*after* rendering its results. All 160 unit tests passed, the page looked
healthy, and the pager silently never appeared. Nothing but loading the page
would have caught it.

So this asserts the two things unit tests structurally cannot: that the browser
reported no errors, and that the DOM the user gets contains what it should.

    ./smoke.py                # build must already exist in dist/
    ./smoke.py --dist other/  # check a different directory
    ./smoke.py --keep-open    # leave the server up for manual poking

Requires Google Chrome. Serves dist/ on an ephemeral port, drives Chrome
headless, and cleans both up on every exit path — a crashed headless Chrome
never exits on its own, and this runs on a laptop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
DEFAULT_DIST = HERE / "dist"

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# Console noise that is not the page's fault and must not fail a deploy.
IGNORABLE = re.compile(
    r"favicon|DevTools|Autofill|GPU|gpu_|Fontconfig|dbus|sandbox|"
    r"Failed to load resource.*favicon", re.I)


class SmokeError(AssertionError):
    pass


def find_chrome() -> str:
    # CI installs Chrome somewhere unpredictable, so an explicit path wins.
    explicit = os.environ.get("CHROME_PATH")
    if explicit and Path(explicit).is_file():
        return explicit
    for path in CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    found = (shutil.which("google-chrome") or shutil.which("chromium")
             or shutil.which("chromium-browser"))
    if found:
        return found
    raise SmokeError(
        "no Chrome found. Set CHROME_PATH, or on CI use "
        "browser-actions/setup-chrome; locally install Google Chrome.")


# A position Chrome will never volunteer. Headless has no way to answer a
# permission prompt and no flag to grant one, and driving CDP to override the
# geolocation would be a devtools client this file otherwise does not need. So
# the API is replaced before app.js loads, in a throwaway copy of the build.
#
# This tests our handling of a fix, not the browser's ability to produce one -
# which is the half that has actually been wrong. "Near me" once ranked by the
# alphabet and called it distance, and every unit test passed while it did.
LONDON_STUB = """<script>
navigator.geolocation.getCurrentPosition = (ok) => ok({
  coords: { latitude: 51.5074, longitude: -0.1278, accuracy: 1200 } });
navigator.permissions.query = async () => ({ state: "granted" });
</script>
"""

MODULE_TAG = '<script type="module" src="app.js"></script>'


@contextmanager
def located(dist: Path):
    """Serve a copy of `dist` whose browser always reports it is in London."""
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        shutil.copytree(dist, site)
        page = site / "index.html"
        html = page.read_text()
        if MODULE_TAG not in html:
            raise SystemExit("smoke: index.html no longer loads app.js as a "
                             "module; the geolocation stub cannot be injected")
        page.write_text(html.replace(MODULE_TAG, LONDON_STUB + MODULE_TAG))
        with serving(site) as base:
            yield base


@contextmanager
def serving(directory: Path):
    """Serve `directory` on a free port, and always shut it down."""
    class Quiet(SimpleHTTPRequestHandler):
        # Subclass rather than assigning to the partial: setting an attribute
        # on a functools.partial does nothing, so the request log kept printing.
        def log_message(self, *args):
            pass

        # SimpleHTTPRequestHandler sends Last-Modified and nothing else, so a
        # browser is free to apply heuristic freshness and keep serving app.js
        # from an earlier build. That cost an afternoon once: a bug had been
        # fixed, the page kept the old behaviour, and the hunt went looking at
        # the operating system. --keep-open hands this server to a human, so it
        # has to be honest about freshness. serve.py already does the same.
        def end_headers(self):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

    handler = partial(Quiet, directory=str(directory))
    # Port 0: the kernel picks one and the server holds it atomically. Probing
    # for a free port first and binding it after leaves a window for a race.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def render(chrome: str, url: str, timeout: int = 60) -> tuple[str, str]:
    """Load a URL headless and return (dom, stderr).

    Chrome writes page console messages to stderr under --enable-logging, which
    is the only way to see an uncaught exception without attaching a devtools
    client - and uncaught exceptions are the whole reason this file exists.

    Deliberately no --user-data-dir: pointing it at a fresh temporary profile
    makes Chrome hang indefinitely rather than dump and exit, which then leaves
    a few hundred MB of browser behind. Measured: minimal 2.2s, --no-sandbox
    2.1s, --enable-logging 2.2s, temp profile never returned.
    """
    try:
        proc = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--enable-logging=stderr", "--v=0",
             "--virtual-time-budget=8000", "--dump-dom", url],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Never leave a wedged browser holding memory on a laptop.
        subprocess.run(["pkill", "-f", "Google Chrome.*headless"],
                       capture_output=True)
        raise SmokeError(f"Chrome did not return within {timeout}s for {url}")
    return proc.stdout, proc.stderr


def console_errors(stderr: str) -> list[str]:
    """Page-level errors only.

    Chrome tags messages from the page's console with ":CONSOLE:" and its own
    internal complaints with ":ERROR:" — socket teardown, GPU probing, network
    service chatter. Matching any line containing "ERROR" swept those in too,
    which made this gate fail roughly one run in eight on a build that was
    perfectly fine. An intermittent gate is worse than none: it teaches you to
    re-run instead of read. So require the CONSOLE tag, and let Chrome grumble
    about its own plumbing in peace.
    """
    errors = []
    for line in stderr.splitlines():
        if ":CONSOLE:" not in line or IGNORABLE.search(line):
            continue
        if re.search(r"Uncaught|ReferenceError|TypeError|SyntaxError|"
                     r"RangeError|is not defined|is not a function", line):
            errors.append(line.strip())
    return errors


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}"
          + (f"\n         {detail}" if detail and not condition else ""))
    return condition


def check_located(chrome: str, base: str) -> list[str]:
    """The half of Near me that needs a position: run against `located()`."""
    failures: list[str] = []

    def assert_(name: str, condition: bool, detail: str = ""):
        if not check(name, condition, detail):
            failures.append(name)

    dom, stderr = render(chrome, f"{base}/?tab=nearme")
    errors = console_errors(stderr)
    assert_("located: no console errors", not errors, "; ".join(errors[:2]))

    away = re.findall(r'class="away"[^>]*>([^<]+)<', dom)
    assert_("located: distances are shown", len(away) > 0)

    names = re.findall(r"<h3>([^<]+)</h3>", dom)
    assert_("located: the nearest venue is first",
            bool(names) and "BFI IMAX" in names[0],
            f"first card is {names[0] if names else 'missing'!r}, expected the BFI")

    # Ascending, and read as numbers: "< 1 km" sorts before "25 km" as text too,
    # but "9 km" would not, and that is exactly the bug worth catching.
    def km(text: str) -> float:
        # "< 1 km" arrives from --dump-dom as "&lt; 1 km".
        text = text.replace("&lt;", "<").strip()
        return 0.5 if text.startswith("<") else float(text.split()[0].replace(",", ""))

    values = [km(a) for a in away]
    assert_("located: distances ascend down the page", values == sorted(values),
            f"first five: {values[:5]}")

    # The country pin has to come off, or "nearest" means "nearest in the
    # country the browser guessed" - which is how Delhi outranked Mumbai.
    countries = set(re.findall(r'class="where">.*?</svg>[^<]*?([A-Z][^<,]*)<', dom))
    assert_("located: ranking is worldwide, not pinned to one country",
            len(countries) > 1 or len(away) < 25, f"countries on page one: {countries}")
    return failures


def run_checks(chrome: str, base: str, expected_venues: int) -> list[str]:
    failures: list[str] = []

    def assert_(name: str, condition: bool, detail: str = ""):
        if not check(name, condition, detail):
            failures.append(name)

    # --- the front page --------------------------------------------------- #
    # A bare visit opens scoped to the country the browser's timezone implies.
    # `country=any` is the explicit way to say everywhere, and the only thing
    # that survives a reload - absence of the parameter means "guess again".
    dom, stderr = render(chrome, base + "/?country=any")
    errors = console_errors(stderr)
    assert_("no uncaught console errors", not errors, "; ".join(errors[:3]))

    cards = len(re.findall(r'<article class="venue', dom))
    assert_("venue cards rendered", cards > 0, f"found {cards}")
    assert_("first page is a full page of 25", cards == 25, f"found {cards}")

    pager = re.search(r'<span class="pageinfo">([^<]*)', dom)
    assert_("pager is present", pager is not None)
    if pager:
        assert_("pager counts the whole dataset",
                str(expected_venues) in pager.group(1),
                f"pager says {pager.group(1)!r}, data has {expected_venues}")

    assert_("tab bar rendered", dom.count("data-tab=") == 3,
            f"found {dom.count('data-tab=')} tabs")
    assert_("title is set", "FindMaxScreen" in dom)
    assert_("licence attribution present", "CC" in dom and "BY-SA" in dom)

    # A link naming a country wins; a link saying nothing about countries gets
    # the guess, which is what keeps the front page's own URL empty.
    bare, _ = render(chrome, base + "/")
    bare_cards = len(re.findall(r'<article class="venue', bare))
    assert_("a bare visit scopes to the visitor's country",
            0 < bare_cards < expected_venues, f"showed {bare_cards} of {expected_venues}")
    assert_("the masthead is the way back to the start", 'id="home"' in bare)
    assert_("My Country answers the 15/70 question in All venues",
            "verdict-a" in bare and "15/70 mm film IMAX in" in bare)

    # --- the sections ----------------------------------------------------- #
    for tab, marker, label in (
        ("nearme", "verdict", "Near me section renders its banner"),
        ("types", "typeshead", "IMAX types section renders the guide"),
    ):
        tab_dom, tab_err = render(chrome, f"{base}/?tab={tab}")
        tab_errors = console_errors(tab_err)
        assert_(f"{tab}: no console errors", not tab_errors,
                "; ".join(tab_errors[:2]))
        assert_(label, marker in tab_dom)

    # Links shared before the sections were reorganised must still land
    # somewhere deliberate rather than silently falling through to All venues.
    for old, expect in (("country", "nearme"), ("film70", "all")):
        aliased, _ = render(chrome, f"{base}/?tab={old}")
        on = re.search(r'data-tab="([a-z0-9]+)"[^>]*class="on"', aliased) \
            or re.search(r'data-tab="([a-z0-9]+)"[^>]*aria-selected="true"', aliased)
        assert_(f"?tab={old} still lands on {expect}",
                on is not None and on.group(1) == expect,
                f"landed on {on.group(1) if on else 'nothing'}")

    # --- Near me, which must never pass the alphabet off as distance ------ #
    # Headless grants no permission, so this is the state every first-time
    # visitor is in. The bug this guards against shipped with 21 green checks:
    # the section showed Delhi above Mumbai to a reader in Mumbai, in country
    # order, under a heading promising proximity.
    near, _ = render(chrome, f"{base}/?tab=nearme")
    assert_("Near me claims no distance without a position",
            'class="away"' not in near,
            "distance labels rendered with no position granted")
    assert_("Near me asks rather than pretending",
            "Where are you?" in near or "ranks all" in near)

    # --- reporting, which is only present once a repo is configured ------- #
    # Reporting lives in the footer, not on every card: 476 copies of a link
    # nobody clicks, pointing at a theatre the reader is already looking at.
    assert_("no per-venue report links", dom.count(">Report<") == 0,
            f"found {dom.count('>Report<')} — reporting belongs in the footer")
    assert_("the footer offers a prefilled report", "mailto:" in dom)
    assert_("the footer explains where corrections go", "Found a mistake" in dom)
    assert_("the report asks which theatre",
            "Which%20theatre" in dom or "Which theatre" in dom)

    # One Google Maps link, not two: the place card already has Directions.
    assert_("maps link present", dom.count(">Maps<") > 0)
    assert_("no separate directions link", dom.count(">Directions<") == 0,
            f"found {dom.count('>Directions<')}")

    # --- the thing that must not be published ----------------------------- #
    try:
        with urlopen(f"{base}/admin.html", timeout=10) as resp:
            status = resp.status
    except Exception as exc:
        status = getattr(exc, "code", 0)
    assert_("admin.html is not in the bundle", status == 404,
            f"got HTTP {status}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST)
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the server running for manual inspection")
    args = parser.parse_args(argv)

    if not (args.dist / "index.html").is_file():
        print(f"no build at {args.dist} — run ./export.py first", file=sys.stderr)
        return 1

    data = json.loads((args.dist / "data" / "venues.json").read_text())
    expected = data["stats"]["venues"]

    chrome = find_chrome()
    print(f"smoke-testing {args.dist} ({expected} venues) with headless Chrome\n")

    started = time.monotonic()
    with serving(args.dist) as base:
        failures = run_checks(chrome, base, expected)

    # A second server, over a copy of the build with the geolocation stubbed.
    # Separate because the stub must not be in the directory --keep-open hands
    # to a human: it would look like the real site and lie about where they are.
    with located(args.dist) as base:
        failures += check_located(chrome, base)

    if args.keep_open:
        with serving(args.dist) as base:
            print(f"\nserving {base} — ctrl-c to stop")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass

    print(f"\n{'FAILED' if failures else 'passed'} in "
          f"{time.monotonic() - started:.1f}s")
    if failures:
        print("failing checks: " + ", ".join(failures))
        return 1

    # A headless Chrome that crashed will not exit by itself, and this runs on
    # a laptop; say so rather than leaving hundreds of MB behind silently.
    leftover = subprocess.run(["pgrep", "-f", "headless"],
                              capture_output=True, text=True).stdout.strip()
    if leftover:
        print(f"warning: headless browser processes survived: {leftover}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
