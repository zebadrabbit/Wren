import asyncio
import email
import email.utils
import imaplib
import logging
from .. import config
from .. import router
from . import gmail_state

PLUGIN_NAME = "Gmail (IMAP) Watcher"
ROLE = "input"   # input-only: watches an inbox, never sends; notify a chat plugin via router.notify_name
# Declared, not defaulted: run.py, config and the reminder skill all read a
# missing CAN_NOTIFY as True, so without this a watcher passed as NOTIFY_VIA.
CAN_NOTIFY = False

def is_active() -> bool:
    return bool(config.EMAIL_WATCH)


def inactive_reason() -> str:
    return "EMAIL_WATCH is empty — no senders are being watched."

# imaplib is blocking and, without a timeout, waits on a hung server forever.
# Every call below goes through asyncio.to_thread so a slow IMAP host costs one
# worker thread, not the event loop every surface shares; the timeout means it
# does not cost that thread for good either.
_IMAP_TIMEOUT = 30

def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, timeout=_IMAP_TIMEOUT)
    conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
    conn.select("INBOX")
    return conn

def _sender_address(raw_from: str) -> str:
    _, addr = email.utils.parseaddr(raw_from)
    return addr.lower()

async def _poll_once(imap_conn) -> None:
    status, data = await asyncio.to_thread(imap_conn.search, None, "UNSEEN")
    if status != "OK":
        return
    for num in data[0].split():
        status, msg_data = await asyncio.to_thread(imap_conn.fetch, num, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        sender = _sender_address(msg.get("From", ""))
        contact = config.EMAIL_WATCH.get(sender)
        if contact:
            subject = msg.get("Subject", "(no subject)")
            if config.EMAIL_DIGEST:
                # one line in tomorrow's briefing, not a ping now
                gmail_state.add(contact, sender, subject)
            else:
                await router.notify_name(contact, f"Email from {sender}: {subject}")
        await asyncio.to_thread(imap_conn.store, num, "+FLAGS", "\\Seen")

async def start() -> None:
    if not config.EMAIL_WATCH:
        return
    while True:
        try:
            conn = await asyncio.to_thread(_connect)
            try:
                await _poll_once(conn)
            finally:
                await asyncio.to_thread(conn.logout)
        except Exception as e:
            logging.warning(f"email watcher poll failed: {e}")
        await asyncio.sleep(config.EMAIL_POLL_SECONDS)
