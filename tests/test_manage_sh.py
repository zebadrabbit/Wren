"""manage.sh's .env editing — run against a scratch file via WREN_ENV_FILE.

Only the secret/token commands are exercised; the service verbs are plain
systemctl passthroughs and would need sudo.
"""
import os
import re
import subprocess
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent.parent / "manage.sh")


def run(env_file, *args, stdin=None):
    return subprocess.run(
        ["bash", SCRIPT, *args], input=stdin, text=True, capture_output=True,
        env={**os.environ, "WREN_ENV_FILE": str(env_file)},
    )


def test_secret_set_writes_env_without_echoing_the_value(tmp_path):
    env = tmp_path / "env"
    env.write_text("WREN_OWNER_ID=1\nGITHUB_TOKEN=old\n")
    r = run(env, "secret", "set", "GITHUB_TOKEN", stdin="ghp_supersecret\n")
    assert r.returncode == 0
    assert "ghp_supersecret" not in r.stdout + r.stderr
    text = env.read_text()
    assert "GITHUB_TOKEN=ghp_supersecret" in text
    assert "GITHUB_TOKEN=old" not in text
    assert "WREN_OWNER_ID=1" in text            # unrelated lines untouched
    assert env.stat().st_mode & 0o077 == 0  # never group/world readable


def test_secret_list_masks_values_and_refuses_unknown_keys(tmp_path):
    env = tmp_path / "env"
    env.write_text("IMAP_PASSWORD=hunter2\n")
    r = run(env, "secret", "list")
    assert r.returncode == 0
    assert "hunter2" not in r.stdout
    assert re.search(r"IMAP_PASSWORD\s+set", r.stdout)
    assert re.search(r"DISCORD_TOKEN\s+not set", r.stdout)
    r = run(env, "secret", "set", "OWNER_NAME", stdin="x\n")
    assert r.returncode != 0               # not a secret; the UI handles it


def test_token_add_list_revoke_roundtrip(tmp_path):
    env = tmp_path / "env"
    env.write_text("WREN_TOKENS=existing:1\n")
    r = run(env, "token", "add", "42")
    assert r.returncode == 0
    tok = re.search(r"^  ([0-9a-f]{48})$", r.stdout, re.M).group(1)
    assert f"WREN_TOKENS=existing:1,{tok}:42" in env.read_text()

    r = run(env, "token", "list")
    assert tok not in r.stdout             # masked
    assert tok[:4] in r.stdout and "42" in r.stdout

    r = run(env, "token", "revoke", "42")
    assert r.returncode == 0
    assert env.read_text().count("WREN_TOKENS=existing:1") == 1
    assert tok not in env.read_text()

    r = run(env, "token", "revoke", "42")  # already gone
    assert r.returncode != 0


def test_a_readable_env_file_is_warned_about(tmp_path):
    env = tmp_path / "env"
    env.write_text("IMAP_PASSWORD=hunter2\n")
    os.chmod(env, 0o664)
    assert "chmod 600" in run(env, "secret", "list").stderr
    os.chmod(env, 0o600)
    assert "chmod 600" not in run(env, "secret", "list").stderr
