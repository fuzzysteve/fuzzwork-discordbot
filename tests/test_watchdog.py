import datetime

import pytest
from click.testing import CliRunner

from fuzzworkbot import update_check, watchdog
from fuzzworkbot.db import now_utc


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(update_check, "_post", lambda token, channel_id, content: sent.append((channel_id, content)))
    return sent


def _run(*args):
    result = CliRunner().invoke(watchdog.main, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def _beat(minutes_ago):
    watchdog.HEARTBEAT_FILE.write_text((now_utc() - datetime.timedelta(minutes=minutes_ago)).isoformat())


def test_fresh_heartbeat_posts_nothing(posts):
    _beat(1)
    assert "Poller OK" in _run()
    assert posts == []


def test_stale_heartbeat_alerts_once_then_recovers(posts):
    _beat(30)
    _run()
    _run()  # still down: no second alert
    assert len(posts) == 1
    channel_id, content = posts[0]
    assert channel_id == watchdog.DEFAULT_CHANNEL_ID
    assert "stopped" in content and "30 min ago" in content

    _beat(0)
    _run()
    assert len(posts) == 2 and "running again" in posts[1][1]
    _run()
    assert len(posts) == 2


def test_missing_heartbeat_counts_as_stale(posts):
    _run()
    assert "no heartbeat recorded" in posts[0][1]


def test_dry_run_posts_nothing_and_keeps_state(posts):
    _beat(30)
    assert "stopped" in _run("--dry-run")
    assert posts == [] and not watchdog.STATE_FILE.exists()


def test_write_then_read_heartbeat():
    watchdog.write_heartbeat()
    assert abs(watchdog.read_heartbeat() - now_utc()) < datetime.timedelta(seconds=5)
