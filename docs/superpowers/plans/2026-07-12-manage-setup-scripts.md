# manage.sh / setup.sh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `manage.sh` (start/stop/restart/status/logs wrapper around the `wren` systemd service) and `setup.sh` (interactive fresh-checkout setup: venv, deps, `.env`, optional systemd install).

**Architecture:** Two independent bash scripts at the repo root. `manage.sh` has no dependency on `setup.sh` or vice versa — either can be written first. This plan does `manage.sh` first since it's the smaller, simpler script.

**Tech Stack:** Bash (`set -euo pipefail`), `systemctl`/`journalctl` (manage.sh), `python3 -m venv`/`pip` (setup.sh). No new dependencies.

## Global Constraints

- Both scripts live at the repo root, executable (`chmod +x`).
- `manage.sh` assumes `wren.service` is already installed under systemd as `wren` — no PID-file/standalone process management.
- `setup.sh` never silently overwrites an existing `.env` — it warns and requires explicit `y` confirmation first.
- `setup.sh`'s systemd install step is a separate, explicitly confirmed step (needs `sudo`), not bundled automatically into the rest of setup.
- Neither script is unit-tested in `pytest` — verification is `bash -n <script>` (syntax check) plus a manual walkthrough, per this project's established pattern for non-Python-testable pieces (e.g. `bot.py`).

---

### Task 1: `manage.sh`

**Files:**
- Create: `manage.sh`

**Interfaces:**
- Produces: a CLI, `./manage.sh {start|stop|restart|status|logs}`.

- [ ] **Step 1: Write `manage.sh`**

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

- [ ] **Step 2: Make it executable**

```bash
chmod +x manage.sh
```

- [ ] **Step 3: Syntax check**

Run: `bash -n manage.sh`
Expected: no output, exit code 0

- [ ] **Step 4: Manual verification**

Run `./manage.sh` with no arguments — expected: prints the usage line and exits non-zero. Run `./manage.sh bogus` — same. If a `wren` systemd service happens to be installed on this machine, run `./manage.sh status` and confirm it reaches `systemctl` (output resembles normal `systemctl status wren` output, not a bash error) — if no such service is installed here, it's acceptable for this step to show `systemctl`'s own "unit not found" error rather than a script error; record which case applies in your report.

- [ ] **Step 5: Commit**

```bash
git add manage.sh
git commit -m "feat: add manage.sh systemctl wrapper for the wren service"
```

---

### Task 2: `setup.sh`

**Files:**
- Create: `setup.sh`

**Interfaces:**
- Produces: a CLI, `./setup.sh` — interactive, no arguments.
- Consumes: `requirements.txt` (existing), `.env.example`'s variable names (existing — this script must write the same variable names `config.py`/`providers.py` already read), `wren.service` (existing, for the optional systemd-install step).

- [ ] **Step 1: Write `setup.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== Wren setup ==="

# 1. venv + deps
if [ ! -d venv ]; then
    echo "Creating virtualenv..."
    python3 -m venv venv
fi
echo "Installing dependencies..."
venv/bin/pip install -q -r requirements.txt

prompt() {
    local __resultvar=$1
    local __prompt_text=$2
    local __default=${3:-}
    local __input
    if [ -n "$__default" ]; then
        read -rp "$__prompt_text [$__default]: " __input
        __input=${__input:-$__default}
    else
        read -rp "$__prompt_text: " __input
    fi
    printf -v "$__resultvar" '%s' "$__input"
}

prompt_numeric() {
    local __resultvar=$1
    local __prompt_text=$2
    local __input
    while true; do
        read -rp "$__prompt_text: " __input
        if [[ "$__input" =~ ^[0-9]+$ ]]; then
            printf -v "$__resultvar" '%s' "$__input"
            break
        fi
        echo "Please enter a numeric Discord user ID."
    done
}

# 2. core values
prompt DISCORD_TOKEN "Discord bot token"
prompt_numeric OWNER_ID "Owner Discord user ID"
prompt_numeric HUSBAND_ID "Husband Discord user ID"

# 3. LLM providers
VALID_PROVIDERS="lmstudio ollama openai claude openrouter"
while true; do
    prompt LLM_PROVIDERS "LLM providers, comma-separated priority order (available: lmstudio, ollama, openai, claude, openrouter)" "lmstudio"
    ok=true
    IFS=',' read -ra chosen <<< "$LLM_PROVIDERS"
    for p in "${chosen[@]}"; do
        p_trimmed=$(echo "$p" | xargs)
        if [[ ! " $VALID_PROVIDERS " == *" $p_trimmed "* ]]; then
            echo "Unknown provider: $p_trimmed"
            ok=false
        fi
    done
    $ok && break
done

PROVIDER_LINES=""
for p in "${chosen[@]}"; do
    p=$(echo "$p" | xargs)
    case "$p" in
        lmstudio)
            prompt LMSTUDIO_BASE_URL "LM Studio base URL" "http://localhost:1234/v1"
            prompt LMSTUDIO_MODEL "LM Studio model name"
            PROVIDER_LINES+="LMSTUDIO_BASE_URL=$LMSTUDIO_BASE_URL"$'\n'"LMSTUDIO_MODEL=$LMSTUDIO_MODEL"$'\n'
            ;;
        ollama)
            prompt OLLAMA_BASE_URL "Ollama base URL" "http://localhost:11434/v1"
            prompt OLLAMA_MODEL "Ollama model name" "llama3"
            PROVIDER_LINES+="OLLAMA_BASE_URL=$OLLAMA_BASE_URL"$'\n'"OLLAMA_MODEL=$OLLAMA_MODEL"$'\n'
            ;;
        openai)
            prompt OPENAI_API_KEY "OpenAI API key"
            prompt OPENAI_MODEL "OpenAI model" "gpt-4o-mini"
            PROVIDER_LINES+="OPENAI_API_KEY=$OPENAI_API_KEY"$'\n'"OPENAI_MODEL=$OPENAI_MODEL"$'\n'
            ;;
        claude)
            prompt ANTHROPIC_API_KEY "Anthropic API key"
            prompt CLAUDE_MODEL "Claude model" "claude-sonnet-5"
            PROVIDER_LINES+="ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"$'\n'"CLAUDE_MODEL=$CLAUDE_MODEL"$'\n'
            ;;
        openrouter)
            prompt OPENROUTER_API_KEY "OpenRouter API key"
            prompt OPENROUTER_MODEL "OpenRouter model" "meta-llama/llama-3"
            PROVIDER_LINES+="OPENROUTER_API_KEY=$OPENROUTER_API_KEY"$'\n'"OPENROUTER_MODEL=$OPENROUTER_MODEL"$'\n'
            ;;
    esac
done

# 4. Email watcher (optional)
EMAIL_LINES=""
read -rp "Configure the email watcher? (y/N): " want_email
if [[ "$want_email" =~ ^[Yy]$ ]]; then
    prompt IMAP_HOST "IMAP host"
    prompt IMAP_USER "IMAP username"
    prompt IMAP_PASSWORD "IMAP password"
    prompt EMAIL_POLL_SECONDS "Poll interval in seconds" "60"
    prompt EMAIL_WATCH "Watch list (sender:contact,sender:contact,...)"
    EMAIL_LINES="IMAP_HOST=$IMAP_HOST
IMAP_USER=$IMAP_USER
IMAP_PASSWORD=$IMAP_PASSWORD
EMAIL_POLL_SECONDS=$EMAIL_POLL_SECONDS
EMAIL_WATCH=$EMAIL_WATCH
"
fi

# 5. Write .env
if [ -f .env ]; then
    echo "WARNING: .env already exists."
    read -rp "Overwrite it? (y/N): " overwrite
    if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
        echo "Aborting without writing .env."
        exit 1
    fi
fi

{
    echo "DISCORD_TOKEN=$DISCORD_TOKEN"
    echo "OWNER_ID=$OWNER_ID"
    echo "HUSBAND_ID=$HUSBAND_ID"
    echo ""
    echo "LLM_PROVIDERS=$LLM_PROVIDERS"
    echo ""
    printf '%s' "$PROVIDER_LINES"
    if [ -n "$EMAIL_LINES" ]; then
        echo ""
        printf '%s' "$EMAIL_LINES"
    fi
} > .env

echo ".env written."

# 6. Optional systemd install
read -rp "Install and enable the systemd service now? (y/N): " want_systemd
if [[ "$want_systemd" =~ ^[Yy]$ ]]; then
    sudo cp wren.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now wren
    echo "wren.service installed and started."
fi

echo "Setup complete."
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x setup.sh
```

- [ ] **Step 3: Syntax check**

Run: `bash -n setup.sh`
Expected: no output, exit code 0

- [ ] **Step 4: Manual verification in a scratch checkout**

Do NOT run this against the real project's `.env` — copy the repo to a
scratch directory first, or temporarily move the real `.env` aside
(`mv .env /tmp/wren-env-backup`) before testing, and restore it afterward
regardless of outcome.

Run `./setup.sh` and walk through it end-to-end with test values:
- venv gets created (or reused if already present) and dependencies install
  without error.
- Enter a fake Discord token; enter a non-numeric owner ID first to confirm
  it re-prompts, then a numeric one.
- Choose `lmstudio` as the provider, enter a base URL and model.
- Answer "n" to the email watcher prompt.
- Confirm the resulting `.env` contains `DISCORD_TOKEN`, `OWNER_ID`,
  `HUSBAND_ID`, `LLM_PROVIDERS=lmstudio`, `LMSTUDIO_BASE_URL`,
  `LMSTUDIO_MODEL`, and no `IMAP_*`/`EMAIL_*` lines.
- Run it a second time against the same scratch `.env` and confirm it
  warns before overwriting, and answering "n" leaves the file untouched
  (aborts with a non-zero exit).
- Answer "n" to the systemd-install prompt (don't actually install a
  service during this verification) and confirm the script still reports
  "Setup complete." and exits 0.

Restore the real `.env` (`mv /tmp/wren-env-backup .env`) before finishing.

- [ ] **Step 5: Commit**

```bash
git add setup.sh
git commit -m "feat: add setup.sh interactive fresh-checkout configuration"
```

---

## Post-plan verification

- [ ] `bash -n manage.sh && bash -n setup.sh` — both pass with no output.
- [ ] Confirm the real project `.env` (if one exists in this checkout) was not modified by any manual verification step above.
- [ ] Manually confirm the README's setup section and these scripts don't contradict each other (e.g. same env var names, same systemd unit name `wren`).
