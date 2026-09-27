import logging
from wren import config, run
from wren.communication import gmail_plugin, github_plugin


def test_input_watcher_as_notify_via_warns_at_boot(monkeypatch, caplog):
    # Watchers never send. With CAN_NOTIFY defaulted to True they passed this
    # check, and NOTIFY_VIA=gmail (or gmail listed first) ate every reminder.
    for name, plugin in (("gmail", gmail_plugin), ("github", github_plugin)):
        monkeypatch.setattr(config, "NOTIFY_VIA", name)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            run._warn_if_notifications_go_nowhere({name: plugin})
        assert "DISCARDED" in caplog.text, name


def test_a_readable_env_file_is_warned_about(tmp_path, caplog):
    # .env holds every credential the install has; 644/664 lets any local
    # account read them.
    import os
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=x\n")
    os.chmod(env, 0o664)
    with caplog.at_level(logging.WARNING):
        run._warn_if_env_readable(str(env))
    assert "chmod 600" in caplog.text
    caplog.clear()
    os.chmod(env, 0o600)
    with caplog.at_level(logging.WARNING):
        run._warn_if_env_readable(str(env))
        run._warn_if_env_readable("")          # no .env at all: nothing to say
    assert caplog.text == ""
