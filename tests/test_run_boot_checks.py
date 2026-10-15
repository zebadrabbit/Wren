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


def test_notify_any_is_quiet_when_something_can_push(monkeypatch, caplog):
    from wren.communication import http_plugin, telegram_plugin
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    with caplog.at_level(logging.WARNING):
        run._warn_if_notifications_go_nowhere({"http": http_plugin, "telegram": telegram_plugin})
    assert caplog.text == ""


def test_notify_any_warns_when_nothing_loaded_can_push(monkeypatch, caplog):
    from wren.communication import http_plugin
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    with caplog.at_level(logging.WARNING):
        run._warn_if_notifications_go_nowhere({"http": http_plugin})
    assert "only appear in the web chat" in caplog.text


def test_notify_via_http_warns_it_is_web_only(monkeypatch, caplog):
    from wren.communication import http_plugin
    monkeypatch.setattr(config, "NOTIFY_VIA", "http")
    with caplog.at_level(logging.WARNING):
        run._warn_if_notifications_go_nowhere({"http": http_plugin})
    assert "only appear in the web chat" in caplog.text


def test_notify_any_warns_discarded_when_nothing_at_all_can_deliver(monkeypatch, caplog):
    from wren.communication import gmail_plugin
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    with caplog.at_level(logging.WARNING):
        run._warn_if_notifications_go_nowhere({"gmail": gmail_plugin})
    assert "DISCARDED" in caplog.text


def test_a_door_without_its_owner_id_is_warned_about(monkeypatch, caplog):
    # Without SLACK_OWNER_ID the owner is a stranger on Slack, and strangers
    # are dropped without a reply.
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["slack", "matrix"])
    monkeypatch.setattr(config, "SLACK_OWNER_ID", "")
    monkeypatch.setattr(config, "MATRIX_OWNER_ID", "")
    with caplog.at_level(logging.WARNING):
        run._warn_if_owner_unset()
    assert "SLACK_OWNER_ID is not set: your own messages on Slack will be ignored." in caplog.text
    assert "MATRIX_OWNER_ID is not set: your own messages on Matrix will be ignored." in caplog.text


def test_owner_ids_are_quiet_when_set_or_the_door_is_off(monkeypatch, caplog):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["slack"])
    monkeypatch.setattr(config, "SLACK_OWNER_ID", "U0123ABCDEF")
    monkeypatch.setattr(config, "MATRIX_OWNER_ID", "")        # matrix is not running
    with caplog.at_level(logging.WARNING):
        run._warn_if_owner_unset()
    assert caplog.text == ""
