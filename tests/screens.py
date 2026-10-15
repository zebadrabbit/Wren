#!/usr/bin/env python3
"""Screenshot the web chat on a scratch instance. Not a pytest test.

    python3 tests/screens.py /tmp/shots

Boots Wren on a temp database and port, seeds a few rows through the API,
renders the page at 1280 and 390 wide in both themes, and asserts the phone
width never scrolls sideways and a full Today panel leaves room for the
timeline and the composer. Needs Playwright in the python it runs under
(`pip install playwright && playwright install chromium`), which is a dev
tool, not a Wren dependency. The scratch process is stopped by its PID: never
`pkill -f wren.run`, which also matches the live service.
"""
import asyncio, functools, http.server, json, os, pathlib, subprocess, sys, tempfile, threading, time, urllib.request
from datetime import datetime, timedelta, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent
# ponytail: fixed ports, not free ones picked at boot. Two runs at once (or
# anything else on 18790-18791) collide; boot() then times out with the log path.
PORT = 18790
FEED_PORT = PORT + 1                                     # the calendar feed below
TOKEN = "shot"
# The briefing lists reminders due *today* in TIMEZONE, and set_reminder
# refuses a time already past, so the scratch zone is one where it is about
# noon right now: 20:00-22:00 today is then always later today, whenever this
# runs. Etc/GMT signs are inverted (Etc/GMT+11 is UTC-11).
_OFFSET = 12 - time.gmtime().tm_hour                    # -11 .. +12, all valid zones
ZONE = f"Etc/GMT{-_OFFSET:+d}"
TODAY = (datetime.now(timezone.utc) + timedelta(hours=_OFFSET)).date()
SNOWFLAKE = "123456789012345678"                         # past 2**53, like every Discord id


def _api(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def serve_calendar(tmp: pathlib.Path) -> http.server.HTTPServer:
    """Three events today, as an ICS feed on FEED_PORT, so the Today panel
    has a calendar section without the network. Weather is the one section
    this cannot fill offline."""
    feed = tmp / "feed"; feed.mkdir()
    day = f"{TODAY:%Y%m%d}"
    events = "".join(
        f"BEGIN:VEVENT\r\nUID:{i}@shot\r\nDTSTART:{day}T{h:02d}0000\r\nDTEND:{day}T{h + 1:02d}0000\r\n"
        f"SUMMARY:{what}\r\nEND:VEVENT\r\n"
        for i, (h, what) in enumerate(((13, "Dentist"), (15, "School pickup"), (18, "Book club"))))
    (feed / "cal.ics").write_text("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + events + "END:VCALENDAR\r\n")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", FEED_PORT), functools.partial(Quiet, directory=str(feed)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def boot(tmp: pathlib.Path) -> subprocess.Popen:
    # WREN_DB first and always: the fallback is the repo's own wren.db, which
    # is the live service's. LLM_PROVIDERS points at a dead port only because
    # config refuses to start without one; nothing shot here asks the model.
    env = {**os.environ, "WREN_DB": str(tmp / "scratch.db"), "COMMUNICATION_PLUGINS": "http",
           "WREN_TOKENS": f"{TOKEN}:1", "WREN_OWNER_ID": "1", "OWNER_NAME": "Erin",
           "WREN_HTTP_HOST": "127.0.0.1", "WREN_HTTP_PORT": str(PORT),
           "DISCORD_TOKEN": "", "TELEGRAM_TOKEN": "", "BRIEFING_TIME": "", "TIMEZONE": ZONE,
           "CALENDAR_URLS": f"http://127.0.0.1:{FEED_PORT}/cal.ics", "EMAIL_DIGEST": "on",
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
    for item in ("milk", "eggs", "olive oil", "bread", "coffee"):
        _api("/api/dispatch", {"intent": "add_shopping_item", "content": item})
    # a full Today panel: the calendar feed, three reminders due today, six
    # watched mails (five and "…and 1 more.") and the shopping count
    for hour, what in ((20, "water the plants"), (21, "call mum"), (22, "lock the shed")):
        _api("/api/dispatch", {"intent": "set_reminder", "content": what, "when": f"{TODAY}T{hour}:00:00"})
    # the People tab: a Discord id JSON would round, and a Telegram-first person
    _api("/api/contacts", {"alias": "hubby", "surface": "discord", "id": SNOWFLAKE})
    _api("/api/contacts", {"alias": "mum", "surface": "telegram", "id": "5550001"})
    # the Telegram sentence below is only a transcript row; this is what makes
    # the Packing space exist
    _api("/api/dispatch", {"intent": "add_shopping_item", "content": "tent", "list": "packing"})
    _api("/api/dispatch", {"intent": "save_note", "content": "call the plumber", "tags": ["house"]})
    # a notice and a Telegram turn, straight into the scratch DB
    env = {**os.environ, "WREN_DB": str(tmp / "scratch.db")}
    subprocess.run([str(REPO / "venv" / "bin" / "python3"), "-c",
                    "from wren import conversations as c; c.init_db(); h = c.home(1); "
                    "c.add_message(h, 'user', 'add a tent to the packing list', channel='telegram'); "
                    "c.add_message(h, 'assistant', 'Added a tent to packing.', channel='telegram'); "
                    "c.notice(1, '⏰ Reminder: take out the trash', 'telegram'); "
                    "from wren.communication import gmail_state as g; g.init_db(); "
                    "[g.add('owner', s, 'About ' + s) for s in ('School', 'Bank', 'Dentist', 'Library', 'Vet', 'Council')]"],
                   cwd=REPO, env=env, check=True)


async def _assert_room_to_chat(page, where):
    log, bottom, inner = await page.evaluate(
        "[document.querySelector('#log').getBoundingClientRect().height,"
        " document.querySelector('#composer').getBoundingClientRect().bottom, window.innerHeight]")
    print(f"{where}: timeline {log:.0f}px, composer ends at {bottom:.0f} of {inner}")
    assert log > 200, f"the timeline is {log:.0f}px tall at {where}"
    assert bottom <= inner, f"the composer is below the fold at {where}"


async def shoot(out: pathlib.Path):
    from playwright.async_api import async_playwright
    # the invite link's form: the token rides in the fragment, never the query
    url = f"http://127.0.0.1:{PORT}/#token={TOKEN}"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scheme in ("light", "dark"):
            for width, height, name in ((1280, 800, "desktop"), (390, 844, "phone")):
                ctx = await browser.new_context(viewport={"width": width, "height": height},
                                                color_scheme=scheme, is_mobile=width < 700,
                                                # the server's zone, so the top bar and the briefing agree on the day
                                                timezone_id=ZONE)
                page = await ctx.new_page()
                await page.goto(url)
                await page.wait_for_selector("#app.on")
                await page.wait_for_timeout(800)
                assert "token=" not in page.url, "the invite token stayed in the address bar"
                rows = await page.evaluate("document.querySelectorAll('#today .bsec li').length")
                assert rows >= 12, f"the Today panel is not full: {rows} rows (calendar, reminders, mail)"
                if width < 700:
                    wide = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                    assert not wide, f"horizontal overflow at {width}px ({scheme})"
                    await _assert_room_to_chat(page, f"{width}px {scheme}, first visit")
                await page.screenshot(path=str(out / f"{name}-{scheme}.png"))
                if width < 700:
                    # unfolded, a full briefing still leaves the timeline and the composer
                    await page.click("#tfold"); await page.wait_for_timeout(200)
                    await _assert_room_to_chat(page, f"{width}px {scheme}, Today open")
                    await page.screenshot(path=str(out / f"{name}-{scheme}-today.png"))
                if width >= 700 and scheme == "light":
                    await page.click("#gear")
                    await page.click("#ptabs .tab[data-tab=people]")
                    await page.wait_for_selector("#pcontacts .prow")
                    people = await page.inner_text("#pcontacts")
                    assert SNOWFLAKE in people, f"the Discord id was mangled: {people!r}"
                    assert "signs in as 5550001" in people, f"no sign-in id on the Telegram row: {people!r}"
                    await page.screenshot(path=str(out / f"{name}-people.png"))
                    await page.click("#pclose")
                if width < 700:
                    await page.click("#menu"); await page.wait_for_timeout(300)
                    await page.screenshot(path=str(out / f"{name}-{scheme}-drawer.png"))
                await ctx.close()
        await browser.close()


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp())
    feed = serve_calendar(tmp)
    proc = boot(tmp)
    try:
        seed(tmp)
        asyncio.run(shoot(out))
        print(f"screens in {out}")
    finally:
        stop(proc)
        feed.shutdown()


if __name__ == "__main__":
    main()
