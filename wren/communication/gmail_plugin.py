import asyncio
import email
import email.utils
import imaplib
import logging
from .. import config
from .. import router

PLUGIN_NAME = "Gmail (IMAP) Watcher"
ROLE = "input"   # input-only: watches an inbox, never sends; notify a chat plugin via router.notify_name

def is_active() -> bool:
    return bool(config.EMAIL_WATCH)

def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST)
    conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
    conn.select("INBOX")
    return conn

def _sender_address(raw_from: str) -> str:
    _, addr = email.utils.parseaddr(raw_from)
    return addr.lower()

async def _poll_once(imap_conn) -> None:
    status, data = imap_conn.search(None, "UNSEEN")
    if status != "OK":
        return
    for num in data[0].split():
        status, msg_data = imap_conn.fetch(num, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        sender = _sender_address(msg.get("From", ""))
        contact = config.EMAIL_WATCH.get(sender)
        if contact:
            subject = msg.get("Subject", "(no subject)")
            await router.notify_name(contact, f"Email from {sender}: {subject}")
        imap_conn.store(num, "+FLAGS", "\\Seen")

async def start() -> None:
    if not config.EMAIL_WATCH:
        return
    while True:
        try:
            conn = _connect()
            try:
                await _poll_once(conn)
            finally:
                conn.logout()
        except Exception as e:
            logging.warning(f"email watcher poll failed: {e}")
        await asyncio.sleep(config.EMAIL_POLL_SECONDS)
