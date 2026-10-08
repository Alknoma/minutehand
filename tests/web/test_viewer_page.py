"""The viewer's page in a browser: headless Chromium (Playwright) against `minutehand view`'s app on fixture runs.

Run with `uv run playwright install chromium` once and `uv run pytest -m browser tests/web`. Without Playwright's
Chromium, the Chrome installed on this machine is used; with neither, each test is skipped, unless
VIEWER_BROWSER_REQUIRED is set (as CI sets it), which fails it instead.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, Error, Page, sync_playwright

from minutehand.adapters.web.app import create_app
from tests.web import long_run

pytestmark = [pytest.mark.browser, pytest.mark.timeout(180)]

COUNT_LISTENERS = """
(() => {
  // every listener added to what lives as long as the page: the window, the document, and the shell's own elements
  window.__lasting = 0;
  const shell = new Set(["top", "picker", "picker-panel", "picker-filter", "picker-list", "identity", "headline",
    "problems", "body", "views", "timeline", "view", "inspector", "tooltip", "help", "theme", "help-button"]);
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, listener, options) {
    if (this === window || this === document || (this instanceof Element && shell.has(this.id))) { window.__lasting += 1; }
    return add.call(this, type, listener, options);
  };
})();
"""


def _serve(state: Path) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except Error:
            try:
                launched = p.chromium.launch(channel="chrome")
            except Error as e:
                if "VIEWER_BROWSER_REQUIRED" in os.environ:
                    pytest.fail(f"no Chromium to drive the page (VIEWER_BROWSER_REQUIRED): {e}")
                pytest.skip("neither Playwright's Chromium nor Chrome is installed: uv run playwright install chromium")
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def days(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Sixty days of the synthetic run, served."""
    state = tmp_path_factory.mktemp("days")
    long_run.write(state, days=60)
    yield from _serve(state)


def _open(browser: Browser, url: str, *, counting: bool = False) -> tuple[Page, list[str]]:
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    if counting:
        page.add_init_script(COUNT_LISTENERS)
    page.goto(url)
    page.wait_for_function("window.__viewerLoaded", timeout=30000)
    return page, errors


def test_the_page_draws_the_run_its_verdict_numbers_findings_timeline_and_every_view_without_a_script_error(
    browser: Browser, days: str
) -> None:
    page, errors = _open(browser, days)
    assert page.locator(".verdict").inner_text().lower() == "failed"
    assert page.locator(".identity h1").inner_text() == "year of follow ups"
    assert page.locator("#headline a").count() == 7 and page.locator(".problem").count() > 0
    box = page.locator(".tl-canvas-box canvas").bounding_box()
    assert box is not None and box["width"] > 600 and box["height"] > 200
    labels = page.locator(".tl-labels [data-lane]").all_inner_texts()
    assert any(label.startswith("Rosa Lind") for label in labels) and any(
        label.startswith("Dispatch") for label in labels
    )

    views = page.locator(".views a[data-view]")
    names = [views.nth(i).get_attribute("data-view") for i in range(views.count())]
    assert len(names) == 13
    for name in names:
        page.click(f".views a[data-view={name}]")
        page.wait_for_function("!document.querySelector('#view').textContent.startsWith('Reading')")
        assert page.locator("#view .measure, #view .empty").count() > 0, name

    page.click(".problem >> nth=0")
    page.wait_for_selector(".insp-kind:has-text('Finding')")
    page.keyboard.press("j")
    page.wait_for_function("location.hash.includes('sel=') && !location.hash.includes('sel=finding')")
    page.wait_for_selector(".insp-head")
    assert errors == []


def test_redrawing_never_adds_a_listener_to_what_outlives_the_draw(browser: Browser, days: str) -> None:
    """The old page bound a click listener to its main element on every draw, so every click that redrew (a clock
    switch, a refresh of a running run) doubled the work of the next one until the tab hung: 14 switches took 4 s.
    Here every listener on the window, the document and the page's shell is bound once."""
    page, errors = _open(browser, days, counting=True)
    page.wait_for_timeout(300)
    bound = page.evaluate("window.__lasting")
    took: list[float] = []
    for _ in range(12):
        began = time.monotonic()
        page.click("[data-clock=real]")
        page.click("[data-clock=sim]")
        took.append(time.monotonic() - began)
    for view in ("calls", "memory", "conversations", "findings", "dispatch", "cost"):
        page.click(f".views a[data-view={view}]")
    for n in range(4):
        page.click(f".problem >> nth={n}")
    run = page.evaluate("location.hash.match(/run=([^&]+)/)[1]")
    for _ in range(3):
        page.evaluate(f"location.hash = 'run=other'; location.hash = 'run={run}'")
        page.wait_for_function("window.__viewerLoaded")
    page.click("#theme")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__lasting") == bound
    assert max(took[6:]) < 2 * max(took[:6]) + 0.2, took
    assert errors == [] or all("other" in e for e in errors), errors


@pytest.mark.timeout(400)
def test_a_year_long_run_loads_and_pans_and_zooms_at_interactive_speed(
    browser: Browser, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Four hundred simulated days, about 34,000 marks: the page loads in seconds and each frame of the timeline,
    from the whole year down to an hour, draws in well under a frame budget's worth of tens of milliseconds."""
    state = tmp_path_factory.mktemp("year")
    long_run.write(state, days=400)
    served = _serve(state)
    url = next(served)
    try:
        began = time.monotonic()
        page, errors = _open(browser, url)
        loaded = time.monotonic() - began
        marks = page.evaluate("window.__viewerLoaded.marks")
        page.evaluate("window.__viewerDraws = []")
        for preset in ("year", "month", "week", "day", "hour", "all"):
            page.click(f"[data-preset={preset}]")
            page.wait_for_timeout(350)
        for _ in range(10):
            page.keyboard.press("ArrowRight")
            page.keyboard.press("+")
        page.wait_for_timeout(200)
        draws: list[float] = page.evaluate("window.__viewerDraws")
        # the read model's first build is the server's (minutehand query pays it too); the page's part is after it
        began = time.monotonic()
        page.evaluate("fetch('/api/runs/' + window.__viewerLoaded.run + '/call-rows').then((r) => r.json())")
        built = time.monotonic() - began
        began = time.monotonic()
        page.click(".views a[data-view=calls]")
        page.wait_for_selector("#view .vt-row")
        calls_shown = time.monotonic() - began
        print(
            f"\nyear-long run: {marks} marks, page loaded in {loaded:.2f} s, {len(draws)} frames drawn, slowest "
            f"{max(draws):.1f} ms, median {sorted(draws)[len(draws) // 2]:.1f} ms; read model built in {built:.2f} s, calls "
            f"table drawn in {calls_shown:.2f} s"
        )
        assert marks > 30000 and loaded < 15 and max(draws) < 120 and built < 30 and calls_shown < 2
        assert errors == []
    finally:
        next(served, None)
