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
        echo "Please enter a numeric user ID."
    done
}

# 2. core values
prompt SURFACES "Surfaces to enable, comma-separated (discord, http)" "discord"

DISCORD_TOKEN=""
if [[ ",${SURFACES// /}," == *",discord,"* ]]; then
    prompt DISCORD_TOKEN "Discord bot token"
fi

prompt_numeric WREN_OWNER_ID "Your Wren user id (reuse your Discord/Telegram user id; never change it later)"

WREN_TOKENS=""
if [[ ",${SURFACES// /}," == *",http,"* ]]; then
    if command -v openssl >/dev/null 2>&1; then
        _http_token=$(openssl rand -hex 24)
    else
        _http_token=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
    fi
    WREN_TOKENS="$_http_token:$WREN_OWNER_ID"
    echo "Generated HTTP bearer token: $_http_token"
    echo "  (the desktop voice client needs this as WREN_TOKEN)"
fi

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

# 4b. GitHub watcher (optional)
GITHUB_LINES=""
read -rp "Configure the GitHub watcher? (y/N): " want_github
if [[ "$want_github" =~ ^[Yy]$ ]]; then
    prompt GITHUB_TOKEN "GitHub token (recommended: required for private repos, raises rate limit to 5000/hr)"
    prompt GITHUB_WATCH "Watch list (owner/repo,owner/repo,...)"
    prompt GITHUB_POLL_SECONDS "Poll interval in seconds" "60"
    GITHUB_LINES="GITHUB_TOKEN=$GITHUB_TOKEN
GITHUB_WATCH=$GITHUB_WATCH
GITHUB_POLL_SECONDS=$GITHUB_POLL_SECONDS
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
    echo "SURFACES=$SURFACES"
    if [ -n "$DISCORD_TOKEN" ]; then
        echo "DISCORD_TOKEN=$DISCORD_TOKEN"
    fi
    echo "WREN_OWNER_ID=$WREN_OWNER_ID"
    if [ -n "$WREN_TOKENS" ]; then
        echo "WREN_TOKENS=$WREN_TOKENS"
        echo "# Set WREN_HTTP_HOST=0.0.0.0 to accept clients from other LAN machines."
    fi
    echo ""
    echo "LLM_PROVIDERS=$LLM_PROVIDERS"
    echo ""
    printf '%s' "$PROVIDER_LINES"
    if [ -n "$EMAIL_LINES" ]; then
        echo ""
        printf '%s' "$EMAIL_LINES"
    fi
    if [ -n "$GITHUB_LINES" ]; then
        echo ""
        printf '%s' "$GITHUB_LINES"
    fi
} > .env

echo ".env written."

# 6. Optional systemd install
read -rp "Install and enable the systemd service now? (y/N): " want_systemd
if [[ "$want_systemd" =~ ^[Yy]$ ]]; then
    REPO_DIR="$(pwd)"
    CURRENT_USER="$(whoami)"
    sed -e "s#/home/winter/work/Wren#${REPO_DIR}#g" -e "s/^User=.*/User=${CURRENT_USER}/" wren.service | sudo tee /etc/systemd/system/wren.service > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now wren
    echo "wren.service installed and started."
fi

echo "Setup complete."
