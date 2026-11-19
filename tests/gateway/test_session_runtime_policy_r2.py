"""Round-2 ``config.get runtime_policy`` fixes: identifiers, route source, registries, side effects, digest."""

import asyncio
import json

import hermes_yaml as yaml
import pytest

from gateway import session_config
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket
from tests.gateway.test_session_busy_controls import owner
from tests.gateway.test_session_runtime_policy import (
    LEAKY_URL,
    PRIMARY,
    SENTINELS,
    _read,
)


def _create(ws):
    return rpc(ws, "session.create", request_id="policy", source="tui", toolsets=[])


def _write(home, config):
    (home / "config.yaml").write_text(yaml.safe_dump(config))


def _backups(home):
    # The daemon's own background writes (heartbeat, ledgers) are not this read's; config backups are.
    return sorted(
        str(p.relative_to(home)) + str(p.stat().st_size)
        for p in home.rglob("*")
        if p.is_file() and "backup" in str(p.relative_to(home))
    )


@pytest.mark.platforms("linux")
def test_identity_fields_refuse_containers_env_refs_and_userinfo(tmp_path):
    model = {
        "provider": "openrouter",
        "default": "${SENTINEL-ENV}",
        "api_key": "SENTINEL-KEY",
        "base_url": LEAKY_URL,
    }
    config = {
        "model": model,
        "fallback_providers": [],
        "fallback_model": {
            "provider": "openrouter",
            "api_key": "SENTINEL-KEY",
            "model": {"SENTINEL-MAP": "x"},
        },
    }
    with owner(tmp_path, config) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                raw = json.dumps(await _read(ws))
                policy = json.loads(raw)["result"]["runtime_policy"]
                assert policy["complete"] is False
                assert policy["primary"]["model"] is None
                assert policy["fallback"]["legacy_fallback_model"] is None
                assert policy["fallback"]["chain_complete"] is False
                for sentinel in (*SENTINELS, "SENTINEL-ENV", "SENTINEL-MAP"):
                    assert sentinel not in raw, sentinel

        asyncio.run(probe())


@pytest.mark.platforms("linux")
@pytest.mark.parametrize(
    "api_key_fields",
    [{"api_key": "${SENTINEL-UNSET}"}, {"api_key_env": "SENTINEL-UNSET"}],
)
def test_unresolved_key_reference_is_unknown_not_api_key(tmp_path, api_key_fields):
    model = {"provider": "openrouter", "default": "primary-model", **api_key_fields}
    with owner(tmp_path, {"model": model}) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                policy = (await _read(ws))["result"]["runtime_policy"]
                assert policy["primary"]["credential_kind"] == "unknown", policy
                assert policy["complete"] is False

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_pinned_session_keeps_its_frozen_route_after_a_profile_edit(tmp_path):
    with owner(tmp_path, {"model": {**PRIMARY, "default": "frozen-model"}}) as (
        home,
        _,
        desc,
    ):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await _create(ws))["result"]["session_id"]
                config = yaml.safe_load((home / "config.yaml").read_text())
                config["model"]["default"] = "edited-model"
                _write(home, config)
                pinned = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                live = (await _read(ws))["result"]["runtime_policy"]
                assert pinned["primary"]["model"] == "frozen-model", pinned
                assert live["primary"]["model"] == "edited-model", live

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_unreadable_config_makes_the_projection_incomplete(tmp_path):
    with owner(tmp_path, {"model": PRIMARY}) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                (home / "config.yaml").write_text("model: [unclosed\n")
                policy = (await _read(ws))["result"]["runtime_policy"]
                assert policy["complete"] is False, policy

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_webhook_removed_from_yaml_without_reload_is_still_reported(tmp_path):
    config = {
        "model": PRIMARY,
        "hooks": {"outbound": [{"url": LEAKY_URL, "events": ["post_tool_call"]}]},
    }
    with owner(tmp_path, config) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                await _create(ws)
                _write(home, {"model": PRIMARY})
                webhooks = (await _read(ws))["result"]["runtime_policy"][
                    "outbound_webhooks"
                ]
                assert webhooks == {
                    "count": 1,
                    "events": ["post_tool_call"],
                    "hosts": ["api.example.test"],
                }, webhooks

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_malformed_webhook_entry_is_incomplete(tmp_path):
    config = {
        "model": PRIMARY,
        "hooks": {
            "outbound": [{"url": LEAKY_URL, "events": ["post_tool_call"]}, {"bogus": 1}]
        },
    }
    with owner(tmp_path, config) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                policy = (await _read(ws))["result"]["runtime_policy"]
                assert policy["complete"] is False, policy

        asyncio.run(probe())


def test_home_read_writes_no_backup_where_a_cold_load_does(tmp_path, monkeypatch):
    from hermes_cli.config import load_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"model": PRIMARY}))
    doc, readable = session_config._home_state(tmp_path)
    assert readable is True and doc["model"] == PRIMARY
    assert _backups(tmp_path) == []
    load_config()  # control: the cold load is what writes the good backup this read must not
    assert _backups(tmp_path) != []


def test_enabled_first_party_observer_is_reported(monkeypatch):
    from hermes_cli.plugins import get_plugin_manager

    monkeypatch.setattr(get_plugin_manager(), "_discovered", True)
    monkeypatch.setattr(get_plugin_manager(), "_hooks", {})
    rows, complete = session_config._observers(
        {"telemetry": {"shared_metrics": {"enabled": True}}}
    )
    assert complete is True
    assert [row["name"] for row in rows] == ["shared_metrics"], rows
    assert rows[0]["events"], rows


def test_revision_covers_completeness_and_ignores_observer_order():
    sections = {
        "observer_plugins": [
            {"name": "a", "events": ["x"]},
            {"name": "b", "events": ["y"]},
        ]
    }
    reordered = {"observer_plugins": list(reversed(sections["observer_plugins"]))}
    assert session_config._revision(sections, True) != session_config._revision(
        sections, False
    )
    assert session_config._revision(sections, True) == session_config._revision(
        reordered, True
    )
