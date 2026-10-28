import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fuzzworkbot import update_check

TODAY = date(2026, 10, 28)


@pytest.mark.parametrize(
    "output, expected",
    [
        ("info: Checking for updates...\nWould update uv from v0.12.0 to v0.13.6\n", [("uv", "0.12.0", "0.13.6")]),
        ("success: You're already on version v0.13.6 of uv (the latest version).\n", []),
    ],
)
def test_uv_self_outdated_parses_output(monkeypatch, output, expected):
    monkeypatch.setattr(update_check.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr=output))
    assert update_check._uv_self_outdated() == expected


def test_uv_self_outdated_rejects_unknown_output(monkeypatch):
    monkeypatch.setattr(update_check.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr="huh"))
    with pytest.raises(RuntimeError, match="unrecognised"):
        update_check._uv_self_outdated()


def _section(found, state, today=TODAY):
    new_state = {}
    lines = update_check._section("Py", "uv sync", lambda: found, state, new_state, today)
    return lines, new_state


def test_section_posts_new_update():
    lines, new_state = _section([("requests", "2.0", "2.1")], {})
    assert lines[0] == "**Py** — 1 outdated (`uv sync`)"
    assert "requests" in lines[2] and "2.0 -> 2.1" in lines[2]
    assert new_state == {"Py|requests|2.1": "2026-10-28"}


def test_section_throttles_repeats_to_weekly():
    found = [("requests", "2.0", "2.1")]
    six_days_ago = {"Py|requests|2.1": (TODAY - timedelta(days=6)).isoformat()}
    lines, new_state = _section(found, six_days_ago)
    assert lines == [] and new_state == six_days_ago

    seven_days_ago = {"Py|requests|2.1": (TODAY - timedelta(days=7)).isoformat()}
    lines, new_state = _section(found, seven_days_ago)
    assert lines and new_state == {"Py|requests|2.1": "2026-10-28"}


def test_section_newer_release_is_announced_immediately():
    state = {"Py|requests|2.1": TODAY.isoformat()}
    lines, new_state = _section([("requests", "2.0", "2.2")], state)
    assert lines
    assert new_state == {"Py|requests|2.2": "2026-10-28"}  # resolved 2.1 key forgotten


def test_section_skips_listed_packages(monkeypatch, capsys):
    monkeypatch.setattr(update_check, "SKIPPED_UPDATES", {("Py", "cbcbox"): "known bad"})
    state = {"Py|cbcbox|2.935": "2026-10-01"}  # even if previously posted and due again

    lines, new_state = _section([("CBCBox", "2.929", "2.935"), ("requests", "2.0", "2.1")], state)

    assert "requests" in "\n".join(lines) and "CBCBox" not in "\n".join(lines)
    assert new_state == {"Py|requests|2.1": "2026-10-28"}
    assert "Skipped Py: CBCBox 2.929 -> 2.935 (known bad)" in capsys.readouterr().out


def test_skip_is_scoped_to_its_section(monkeypatch):
    monkeypatch.setattr(update_check, "SKIPPED_UPDATES", {("Other", "cbcbox"): "known bad"})
    lines, _ = _section([("cbcbox", "2.929", "2.935")], {})
    assert lines and "cbcbox" in lines[2]


def test_skipped_entries_are_lowercase_and_name_real_sections():
    titles = {"uv", "Python (bot)", "Python (compression)", "Python (pyapi)", "PHP (discord-auth, live)"}
    for (title, name), reason in update_check.SKIPPED_UPDATES.items():
        assert title in titles and name == name.lower() and reason


def test_section_failure_keeps_state_for_that_section_only():
    state = {"Py|requests|2.1": "2026-10-27", "Other|x|1": "2026-10-27"}
    new_state = {}

    def boom():
        raise RuntimeError("network down")

    lines = update_check._section("Py", "uv sync", boom, state, new_state, TODAY)

    assert lines == ["**Py** — check failed: `RuntimeError: network down`"]
    assert new_state == {"Py|requests|2.1": "2026-10-27"}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """main() with every real check replaced and posting captured."""
    state_file = tmp_path / "state" / "update-check.json"
    monkeypatch.setattr(update_check, "STATE_FILE", state_file)
    monkeypatch.setattr(update_check, "_uv_self_outdated", lambda: [])
    monkeypatch.setattr(update_check, "_python_outdated", lambda project_dir: [])
    monkeypatch.setattr(update_check, "_composer_outdated", lambda: [])
    posts = []
    monkeypatch.setattr(update_check, "_post", lambda token, channel_id, content: posts.append((channel_id, content)))
    return SimpleNamespace(state_file=state_file, posts=posts, monkeypatch=monkeypatch)


def test_main_posts_and_records_state(isolated):
    isolated.monkeypatch.setattr(update_check, "_composer_outdated", lambda: [("guzzlehttp/guzzle", "8.0.2", "8.3.1")])

    result = CliRunner().invoke(update_check.main, [])

    assert result.exit_code == 0, result.output
    [(channel_id, content)] = isolated.posts
    assert channel_id == update_check.DEFAULT_CHANNEL_ID
    assert "guzzlehttp/guzzle" in content
    state = json.loads(isolated.state_file.read_text())
    assert list(state) == ["PHP (discord-auth, live)|guzzlehttp/guzzle|8.3.1"]

    # Second run the same day: nothing new, nothing posted.
    CliRunner().invoke(update_check.main, [])
    assert len(isolated.posts) == 1


def test_main_dry_run_neither_posts_nor_saves(isolated):
    isolated.monkeypatch.setattr(update_check, "_uv_self_outdated", lambda: [("uv", "0.12.0", "0.13.6")])
    result = CliRunner().invoke(update_check.main, ["--dry-run"])
    assert "0.12.0 -> 0.13.6" in result.output
    assert isolated.posts == [] and not isolated.state_file.exists()


def test_main_silent_when_up_to_date(isolated):
    result = CliRunner().invoke(update_check.main, [])
    assert "nothing posted" in result.output and isolated.posts == []


def test_post_truncates_to_discord_limit(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json)
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(update_check.requests, "post", fake_post)
    update_check._post("tok", 123, "x" * 5000)

    assert sent["url"].endswith("/channels/123/messages")
    assert sent["headers"] == {"Authorization": "Bot tok"}
    assert len(sent["json"]["content"]) <= update_check.DISCORD_MESSAGE_LIMIT
    assert sent["json"]["allowed_mentions"] == {"parse": []}
