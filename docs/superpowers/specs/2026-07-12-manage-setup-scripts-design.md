# manage.sh / setup.sh — Design

## Goal

Two small bash scripts, no new dependencies:
- `manage.sh` — start/stop/restart/status/logs for the deployed bot.
- `setup.sh` — one-command setup for a fresh checkout: venv, deps, `.env`,
  optional systemd install.

Wren already has `wren.service` (a systemd unit) and `requirements.txt`.
These scripts wrap what's already there rather than introducing a second
process-management mechanism.

## `manage.sh`

Thin wrapper around `systemctl`/`journalctl` for the `wren` service (the
name `wren.service` is installed under, per the README's deployment
section):

```bash
#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
}

[ $# -eq 1 ] || usage

case "$1" in
    start|stop|restart|status)
        sudo systemctl "$1" wren
        ;;
    logs)
        sudo journalctl -u wren -n 50 -f
        ;;
    *)
        usage
        ;;
esac
```

No PID-file/standalone process management — this assumes `wren.service` is
already installed (`setup.sh`, or the README's manual steps). `logs` shows
the last 50 lines then follows.

## `setup.sh`

Interactive, run once on a fresh checkout. Steps, in order:

1. **venv + deps.** Create `venv/` if it doesn't exist
   (`python3 -m venv venv`), then `venv/bin/pip install -r requirements.txt`.
2. **Core `.env` values.** Prompt for `DISCORD_TOKEN`, `OWNER_ID`,
   `HUSBAND_ID`. IDs are validated as numeric (re-prompt on non-numeric
   input, matching `config.py`'s own validation).
3. **LLM provider(s).** Prompt for an ordered, comma-separated list of
   providers from `lmstudio`/`ollama`/`openai`/`claude`/`openrouter` (free
   text, validated against that fixed set). For each chosen provider,
   prompt for its specific settings (base URL for `lmstudio`; API key +
   model for `openai`/`claude`/`openrouter`; optional base URL/model
   override for `ollama`) — same variable names `providers.py`/`.env.example`
   already document.
4. **Email watcher (optional).** Ask yes/no. If yes: prompt for
   `IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD`, `EMAIL_POLL_SECONDS` (default
   60), and one or more `sender:contact` pairs for `EMAIL_WATCH`. If no,
   these lines are omitted/left commented — the feature stays opt-in.
5. **Write `.env`.** If `.env` already exists, show a warning and require
   an explicit "overwrite? (y/N)" confirmation before replacing it — never
   silently clobber existing configuration.
6. **Optional systemd install.** Ask yes/no. If yes: `sudo cp wren.service
   /etc/systemd/system/`, `sudo systemctl daemon-reload`, `sudo systemctl
   enable --now wren`. This is a separate confirmed step (needs `sudo`),
   not bundled automatically into the rest of setup.

## Testing / verification

Bash scripts aren't unit-tested in this repo's `pytest` suite. Verification
is:
- `bash -n manage.sh` / `bash -n setup.sh` — syntax check, the minimal
  automated safety net for a shell script.
- Manual walkthrough: run `setup.sh` in a scratch checkout (or with
  `.env` temporarily moved aside) and confirm the resulting `.env` matches
  what was entered; run `manage.sh status` against a real installed service
  to confirm it reaches `systemctl` correctly.

## Out of scope

- Non-systemd process management (PID files, `nohup`) — `manage.sh` assumes
  systemd.
- Any change to `wren.service`, `config.py`, or `.env.example` beyond what's
  needed to stay consistent with the variable names these scripts write.
