#!/usr/bin/env python3
"""Screenshot the web chat on a scratch instance. Not a pytest test.

    python3 tests/screens.py /tmp/shots

Boots Wren on a temp database and port, seeds a few rows through the API,
renders the page at 1280 and 390 wide in both themes, and asserts the phone
width never scrolls sideways. Needs Playwright in the python it runs under
(`pip install playwright && playwright install chromium`), which is a dev
tool, not a Wren dependency. The scratch process is stopped by its PID: never
`pkill -f wren.run`, which also matches the live service.
"""
import asyncio, os, pathlib, subprocess, sys, tempfile, time, urllib.request, json

REPO = pathlib.Path(__file__).resolve().parent.parent
# ponytail: a fixed port, not a free one picked at boot. Two runs at once (or
# anything else on 18790) collide; boot() then times out with the log path.
PORT = 18790
TOKEN = "shot"


def _api(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def boot(tmp: pathlib.Path) -> subprocess.Popen:
    # WREN_DB first and always: the fallback is the repo's own wren.db, which
    # is the live service's. LLM_PROVIDERS points at a dead port only because
    # config refuses to start without one; nothing shot here asks the model.
    env = {**os.environ, "WREN_DB": str(tmp / "scratch.db"), "COMMUNICATION_PLUGINS": "http",
           "WREN_TOKENS": f"{TOKEN}:1", "WREN_OWNER_ID": "1", "OWNER_NAME": "Erin",
           "WREN_HTTP_HOST": "127.0.0.1", "WREN_HTTP_PORT": str(PORT),
           "DISCORD_TOKEN": "", "TELEGRAM_TOKEN": "", "BRIEFING_TIME": "",
           "LLM_PROVIDERS": "lmstudio", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9",
           "LMSTUDIO_MODEL": "none"}
    proc = subprocess.Popen([str(REPO / "venv" / "bin" / "python3"), "-m", "wren.run"],
                            cwd=REPO, env=env, stdout=open(tmp / "wren.log", "wb"), stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    stop(proc)
    raise SystemExit("scratch Wren did not come up; see " + str(tmp / "wren.log"))


def stop(proc: subprocess.Popen):
    """By the handle we started, never by name."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def seed(tmp: pathlib.Path):
    for item in ("milk", "eggs", "olive oil"):
        _api("/api/dispatch", {"intent": "add_shopping_item", "content": item})
    _api("/api/dispatch", {"intent": "save_note", "content": "call the plumber", "tags": ["house"]})
    # a notice and a Telegram turn, straight into the scratch DB
    env = {**os.environ, "WREN_DB": str(tmp / "scratch.db")}
    subprocess.run([str(REPO / "venv" / "bin" / "python3"), "-c",
                    "from wren import conversations as c; c.init_db(); h = c.home(1); "
                    "c.add_message(h, 'user', 'add a tent to the packing list', channel='telegram'); "
                    "c.add_message(h, 'assistant', 'Added a tent to packing.', channel='telegram'); "
                    "c.notice(1, '⏰ Reminder: take out the trash', 'telegram')"],
                   cwd=REPO, env=env, check=True)


async def shoot(out: pathlib.Path):
    from playwright.async_api import async_playwright
    # the invite link's form: the token rides in the fragment, never the query
    url = f"http://127.0.0.1:{PORT}/#token={TOKEN}"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scheme in ("light", "dark"):
            for width, height, name in ((1280, 800, "desktop"), (390, 844, "phone")):
                ctx = await browser.new_context(viewport={"width": width, "height": height},
                                                color_scheme=scheme, is_mobile=width < 700)
                page = await ctx.new_page()
                await page.goto(url)
                await page.wait_for_selector("#app.on")
                await page.wait_for_timeout(800)
                assert "token=" not in page.url, "the invite token stayed in the address bar"
                if width < 700:
                    wide = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                    assert not wide, f"horizontal overflow at {width}px ({scheme})"
                await page.screenshot(path=str(out / f"{name}-{scheme}.png"))
                if width < 700:
                    await page.click("#menu"); await page.wait_for_timeout(300)
                    await page.screenshot(path=str(out / f"{name}-{scheme}-drawer.png"))
                await ctx.close()
        await browser.close()


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp())
    proc = boot(tmp)
    try:
        seed(tmp)
        asyncio.run(shoot(out))
        print(f"screens in {out}")
    finally:
        stop(proc)


if __name__ == "__main__":
    main()
