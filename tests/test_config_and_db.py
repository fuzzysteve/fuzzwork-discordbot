import datetime

import pytest
from sqlalchemy import inspect

from fuzzworkbot.config import load_config
from fuzzworkbot.db import make_session_factory, now_utc


def test_load_config_reads_env():
    config = load_config()
    assert config.discord_bot_token == "test-token"
    assert config.guild_id == 1000
    assert config.validated_role_id == 2000


@pytest.mark.parametrize("missing", ["DISCORD_BOT_TOKEN", "GUILD_ID", "VALIDATED_ROLE_ID", "DATABASE_URL", "ESI_USER_AGENT"])
def test_load_config_raises_on_missing_required_var(monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        load_config()


def test_sde_url_defaults_to_eve_schema_with_real_password(monkeypatch):
    # Regression: str(URL) masks the password as "***" — the derived URL must not.
    monkeypatch.delenv("SDE_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://discordbot:s3cret@dbhost/discordbot")
    config = load_config()
    assert config.sde_database_url == "mysql+pymysql://discordbot:s3cret@dbhost/eve"


def test_sde_url_override(monkeypatch):
    monkeypatch.setenv("SDE_DATABASE_URL", "mysql+pymysql://other:pw@elsewhere/eve")
    assert load_config().sde_database_url == "mysql+pymysql://other:pw@elsewhere/eve"


def test_now_utc_is_naive_utc():
    value = now_utc()
    assert value.tzinfo is None
    expected = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs(expected - value) < datetime.timedelta(seconds=5)


def test_make_session_factory_creates_all_tables(db_url):
    session_factory = make_session_factory(db_url)
    tables = set(inspect(session_factory.kw["bind"]).get_table_names())
    assert {"userlookup", "prizes", "giveaway_codes", "giveaways", "giveaway_winners", "role_reactions"} <= tables
