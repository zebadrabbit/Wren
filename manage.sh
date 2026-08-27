#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
# Overridable so tests can point at a scratch file; everything real uses .env.
ENV_FILE="${WREN_ENV_FILE:-.env}"

# Keep in sync with SECRET_KEYS in wren/config.py: the web UI shows these only
# as set / not set. The values live in .env and are managed here, on the host,
# where they never cross the network.
SECRET_KEYS=(DISCORD_TOKEN TELEGRAM_TOKEN WREN_TOKENS IMAP_PASSWORD
             GITHUB_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY
             OPENROUTER_API_KEY FIRECRAWL_API_KEY CALENDAR_URLS)

usage() {
    cat >&2 <<'EOF'
Usage: manage.sh <command>

Service:
  start | stop | restart | status    systemctl wren
  logs                               follow the journal

Secrets — kept in .env on this host; the web UI only ever sees set/not-set:
  secret list                        which credentials are configured
  secret set KEY                     prompt for a value (hidden) and write it
  secret unset KEY                   remove KEY from .env

Web-chat sign-in tokens (the WREN_TOKENS credential, one entry per person):
  token list                         user ids, tokens masked
  token add USER_ID                  generate a token, shown once
  token revoke USER_ID               remove that user's tokens

.env is read at startup: restart wren after changing secrets or tokens.
EOF
    exit 1
}

# ── .env editing ─────────────────────────────────────────────────────────────
# A line-by-line rewrite rather than sed: tokens and passwords contain
# characters that are sed syntax, and printf %s passes any value through
# untouched. The temp file lives next to .env so mv stays on one filesystem.

env_get() {   # KEY -> value on stdout, empty if unset
    [ -f "$ENV_FILE" ] || return 0
    local line
    line=$(grep -m1 "^$1=" "$ENV_FILE" || true)
    printf '%s' "${line#*=}"
}

env_set() {   # KEY VALUE — replace the line or append it, everything else kept
    local key=$1 value=$2 tmp found=0 line
    tmp=$(mktemp "$ENV_FILE.XXXXXX")
    if [ -f "$ENV_FILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            if [[ $line == "$key="* ]]; then
                printf '%s=%s\n' "$key" "$value" >>"$tmp"; found=1
            else
                printf '%s\n' "$line" >>"$tmp"
            fi
        done <"$ENV_FILE"
    fi
    [ "$found" -eq 1 ] || printf '%s=%s\n' "$key" "$value" >>"$tmp"
    # .env holds every credential this install has; never group/world readable
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}

env_unset() {   # KEY — drop the line, everything else kept
    local key=$1 tmp line
    [ -f "$ENV_FILE" ] || return 0
    tmp=$(mktemp "$ENV_FILE.XXXXXX")
    while IFS= read -r line || [ -n "$line" ]; do
        [[ $line == "$key="* ]] || printf '%s\n' "$line" >>"$tmp"
    done <"$ENV_FILE"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}

require_secret_key() {
    local k
    for k in "${SECRET_KEYS[@]}"; do
        [ "$k" = "$1" ] && return 0
    done
    echo "Unknown secret '$1'. Known: ${SECRET_KEYS[*]}" >&2
    exit 1
}

# ── secrets ──────────────────────────────────────────────────────────────────

cmd_secret_list() {
    local k v n
    for k in "${SECRET_KEYS[@]}"; do
        v=$(env_get "$k")
        if [ -z "$v" ]; then
            printf '  %-22s not set\n' "$k"
        elif [ "$k" = WREN_TOKENS ]; then
            n=$(awk -F, '{print NF}' <<<"$v")
            printf '  %-22s set (%s sign-in token(s))\n' "$k" "$n"
        else
            printf '  %-22s set\n' "$k"
        fi
    done
}

cmd_secret_set() {
    require_secret_key "$1"
    local value
    # -s: the value never reaches the terminal, scrollback, or shell history
    read -rsp "Value for $1 (input hidden): " value
    echo
    if [ -z "$value" ]; then
        echo "Empty value — to remove a secret use: $0 secret unset $1" >&2
        exit 1
    fi
    env_set "$1" "$value"
    echo "$1 written to $ENV_FILE (never shown again here or in the web UI)."
    echo "Restart wren to apply: $0 restart"
}

cmd_secret_unset() {
    require_secret_key "$1"
    env_unset "$1"
    echo "$1 removed from $ENV_FILE. Restart wren to apply: $0 restart"
}

# ── web-chat tokens ──────────────────────────────────────────────────────────
# WREN_TOKENS is token:user_id pairs, comma-separated. The user id is the last
# colon field (config._parse_tokens rsplits, so a token containing ':' parses).

cmd_token_list() {
    local raw pair tok
    raw=$(env_get WREN_TOKENS)
    if [ -z "$raw" ]; then
        echo "No web-chat tokens configured."
        return
    fi
    IFS=',' read -ra pairs <<<"$raw"
    for pair in "${pairs[@]}"; do
        pair=$(echo "$pair" | xargs)
        [ -n "$pair" ] || continue
        tok=${pair%:*}
        printf '  user %-20s %s…\n' "${pair##*:}" "${tok:0:4}"
    done
}

cmd_token_add() {
    local id=$1 tok raw
    [[ $id =~ ^[0-9]+$ ]] || { echo "USER_ID must be numeric." >&2; exit 1; }
    tok=$(openssl rand -hex 24 2>/dev/null \
          || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
    raw=$(env_get WREN_TOKENS)
    env_set WREN_TOKENS "${raw:+$raw,}$tok:$id"
    echo "Token for user $id — shown this once; paste it into the web chat sign-in:"
    echo
    echo "  $tok"
    echo
    echo "Restart wren to apply: $0 restart"
}

cmd_token_revoke() {
    local id=$1 raw pair keep=()
    [[ $id =~ ^[0-9]+$ ]] || { echo "USER_ID must be numeric." >&2; exit 1; }
    raw=$(env_get WREN_TOKENS)
    IFS=',' read -ra pairs <<<"${raw:-}"
    for pair in "${pairs[@]}"; do
        [ "$(echo "${pair##*:}" | xargs)" = "$id" ] || keep+=("$pair")
    done
    if [ ${#keep[@]} -eq ${#pairs[@]} ]; then
        echo "No token for user $id." >&2
        exit 1
    fi
    if [ ${#keep[@]} -eq 0 ]; then
        env_unset WREN_TOKENS
    else
        env_set WREN_TOKENS "$(IFS=,; echo "${keep[*]}")"
    fi
    echo "Revoked user $id. Restart wren to apply: $0 restart"
}

# ── dispatch ─────────────────────────────────────────────────────────────────

[ $# -ge 1 ] || usage

case "$1" in
    start|stop|restart|status)
        sudo systemctl "$1" wren
        ;;
    logs)
        sudo journalctl -u wren -n 50 -f
        ;;
    secret)
        case "${2:-}" in
            list)  cmd_secret_list ;;
            set)   [ $# -eq 3 ] || usage; cmd_secret_set "$3" ;;
            unset) [ $# -eq 3 ] || usage; cmd_secret_unset "$3" ;;
            *)     usage ;;
        esac
        ;;
    token)
        case "${2:-}" in
            list)   cmd_token_list ;;
            add)    [ $# -eq 3 ] || usage; cmd_token_add "$3" ;;
            revoke) [ $# -eq 3 ] || usage; cmd_token_revoke "$3" ;;
            *)      usage ;;
        esac
        ;;
    *)
        usage
        ;;
esac
