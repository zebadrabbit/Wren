from wren import settings


def test_get_returns_none_for_unset_key():
    settings.init_db()
    assert settings.get("TIMEZONE") is None


def test_set_then_get_round_trips():
    settings.init_db()
    settings.set("TIMEZONE", "America/Chicago")
    assert settings.get("TIMEZONE") == "America/Chicago"


def test_set_twice_overwrites_rather_than_erroring():
    settings.init_db()
    settings.set("TIMEZONE", "UTC")
    settings.set("TIMEZONE", "America/Chicago")
    assert settings.get("TIMEZONE") == "America/Chicago"


def test_unset_removes_the_row_and_reports_it():
    settings.init_db()
    settings.set("TIMEZONE", "UTC")
    assert settings.unset("TIMEZONE") is True
    assert settings.get("TIMEZONE") is None


def test_unset_reports_false_when_there_was_no_row():
    settings.init_db()
    assert settings.unset("TIMEZONE") is False


def test_all_returns_only_stored_deviations():
    settings.init_db()
    assert settings.all() == {}
    settings.set("TIMEZONE", "UTC")
    settings.set("SEARXNG_URL", "http://searx.lan")
    assert settings.all() == {"TIMEZONE": "UTC", "SEARXNG_URL": "http://searx.lan"}


def test_init_db_is_idempotent():
    settings.init_db()
    settings.set("TIMEZONE", "UTC")
    settings.init_db()
    assert settings.get("TIMEZONE") == "UTC"
