"""Round-4 ``config.get runtime_policy``: complete only when every source a path may read agrees."""

import asyncio
import json
import logging
import os
from pathlib import Path

import hermes_yaml as yaml
import pytest

from gateway import session_config
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket
from tests.gateway.test_session_busy_controls import owner
from tests.gateway.test_session_runtime_policy import PRIMARY, SENTINELS, _read
from tests.gateway.test_session_runtime_policy_r2 import _write

PAGESTONE = {"name": "pagestone", "version": "0.1.0"}
NO_KEY = {k: v for k, v in PRIMARY.items() if k != "api_key"}


def _create_pagestone(ws, cwd, **overrides):
    # The spec's create shape: an acp session, no yolo, an explicit empty scratch cwd, no tools or hooks.
    shape = dict(
        source="acp",
        yolo=False,
        cwd=str(cwd),
        client=PAGESTONE,
        toolsets=[],
        ignore_rules=True,
        accept_hooks=False,
        follow_profile_config=False,
        editor={"mcp_servers": []},
    )
    return rpc(ws, "session.create", request_id="policy", **{**shape, **overrides})


def _scratch(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    return scratch


def _policy_for(home, desc, *, followed=False, **overrides):
    async def probe():
        async with websocket(home, desc) as ws:
            sid = (
                await _create_pagestone(
                    ws,
                    _scratch(home.parent),
                    follow_profile_config=followed,
                    **overrides,
                )
            )["result"]["session_id"]
            return json.dumps(await _read(ws, session_id=sid)), sid

    return asyncio.run(probe())


def test_followed_session_is_incomplete(tmp_path):
    with owner(tmp_path, {"model": PRIMARY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        raw, _ = _policy_for(home, desc, followed=True)
        policy = json.loads(raw)["result"]["runtime_policy"]
        assert policy["complete"] is False, policy


def test_live_edit_disagreeing_with_frozen_fallback_is_incomplete(tmp_path):
    with owner(tmp_path, {"model": PRIMARY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await _create_pagestone(ws, _scratch(tmp_path)))["result"][
                    "session_id"
                ]
                config = yaml.safe_load((home / "config.yaml").read_text())
                config["fallback_providers"] = [
                    {"provider": "openrouter", "model": "live-fb", "api_key": "x"}
                ]
                _write(home, config)
                policy = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                assert policy["complete"] is False, policy
                assert policy["fallback"]["chain_complete"] is False, policy
                assert [row["model"] for row in policy["fallback"]["providers"]] == [
                    "live-fb"
                ], policy

        asyncio.run(probe())


def test_named_provider_is_incomplete_and_unknown(tmp_path):
    named = {
        "providers": {
            "mine": {
                "base_url": "https://named.example.test/v1",
                "api_key": "SENTINEL-KEY",
            }
        }
    }
    with owner(tmp_path, {"model": PRIMARY, "fallback_providers": [], **named}) as (
        home,
        _,
        desc,
    ):
        raw, _ = _policy_for(home, desc)
        policy = json.loads(raw)["result"]["runtime_policy"]
        assert policy["complete"] is False, policy
        assert policy["primary"]["credential_kind"] == "unknown", policy
        assert policy["primary"]["endpoint_host"] == "", policy
        assert "SENTINEL-KEY" not in raw and "named.example.test" not in raw


CATALOG = {
    "openrouter": {
        "name": "OpenRouter",
        "env": ["OPENROUTER_API_KEY"],
        "api": "https://openrouter.ai/api/v1",
        "models": {},
    }
}


def _catalog(home):
    # The declared key vars come from the models.dev catalog; a profile without one reads them as unknown.
    (home / "models_dev_cache.json").write_text(json.dumps(CATALOG))


def _seed_catalog(monkeypatch):
    # The daemon loads its models.dev registry from the profile cache once, at start, and the harness seeds a
    # placeholder there. The catalog must be the seed, or the declared key vars are absent.
    from tests.gateway.fixtures import local_recovery_probe as probe

    monkeypatch.setattr(
        probe, "seed_offline_models_dev", lambda home: _catalog(Path(home))
    )


def test_env_key_without_a_declared_catalog_is_unknown(tmp_path):
    with owner(tmp_path, {"model": NO_KEY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("OPENROUTER_API_KEY=SENTINEL-ENV-KEY\n")
        raw, _ = _policy_for(home, desc)
        policy = json.loads(raw)["result"]["runtime_policy"]
        assert policy["primary"]["credential_kind"] == "unknown", policy
        assert policy["complete"] is False, policy
        assert "SENTINEL-ENV-KEY" not in raw


def test_profile_env_key_is_api_key_by_presence_only(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with owner(tmp_path, {"model": NO_KEY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("OPENROUTER_API_KEY=SENTINEL-ENV-KEY\n")
        raw, _ = _policy_for(home, desc)
        policy = json.loads(raw)["result"]["runtime_policy"]
        assert policy["complete"] is True, policy
        assert policy["primary"]["credential_kind"] == "api_key", policy
        assert policy["primary"]["endpoint_host"] == "api.example.test", policy
        assert "SENTINEL-ENV-KEY" not in raw


def test_env_endpoint_override_makes_host_unknown(tmp_path):
    with owner(tmp_path, {"model": PRIMARY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text(
            "OPENROUTER_BASE_URL=https://override.example.test/v1\n"
        )
        raw, _ = _policy_for(home, desc)
        policy = json.loads(raw)["result"]["runtime_policy"]
        assert policy["primary"]["endpoint_host"] == "", policy
        assert policy["complete"] is False, policy
        assert "override.example.test" not in raw


def test_registry_change_between_samples_is_incomplete(monkeypatch):
    import agent.outbound_webhooks as outbound

    doc = {"model": PRIMARY}
    states = iter([[("post_tool_call", "https://a.example.test/x")], []])
    monkeypatch.setattr(outbound, "registered_targets", lambda: next(states, []))
    monkeypatch.setattr(session_config, "_observers", lambda home_doc: ([], True))
    result = session_config._runtime_policy(
        doc,
        doc,
        chain=doc,
        chain_known=True,
        checkpoint=False,
        secrets=set(),
        bound=False,
        session=True,
        managed=False,
        known=True,
    )
    assert result["complete"] is False, result


def test_malformed_managed_overlay_is_incomplete(monkeypatch):
    from hermes_cli import managed_scope

    def loader(*, strict=False):
        if strict:
            raise ValueError("malformed managed config.yaml")
        return {}

    monkeypatch.setattr(managed_scope, "load_managed_config", loader)
    assert session_config._managed_overlay_active() is True


def test_shell_hook_inventory_is_quiet(caplog):
    doc = {
        "model": PRIMARY,
        "hooks": {"SENTINEL-EVENT-NAME": [{"command": "SENTINEL-CMD"}]},
    }
    with caplog.at_level(logging.DEBUG):
        result = session_config._runtime_policy(
            doc,
            doc,
            chain=doc,
            chain_known=True,
            checkpoint=False,
            secrets=set(),
            bound=False,
            session=True,
            managed=False,
            known=True,
        )
    assert result["complete"] is False, result
    assert "SENTINEL-EVENT-NAME" not in caplog.text
    assert "SENTINEL-CMD" not in caplog.text


def test_resident_activated_fallback_is_detected():
    class Resident:
        _fallback_activated = True

    class Idle:
        _fallback_activated = False

    assert session_config._resident_fallback_active(Resident()) is True
    assert session_config._resident_fallback_active(Idle()) is False
    assert session_config._resident_fallback_active(None) is False


def test_retained_chain_that_disagrees_is_detected(tmp_path, monkeypatch):
    from hermes_cli import config as hermes_config

    home = tmp_path / "state"
    home.mkdir()
    retained = {
        "model": PRIMARY,
        "fallback_providers": [{"provider": "openrouter", "model": "old-fb"}],
    }
    doc = {"model": PRIMARY, "fallback_providers": []}
    monkeypatch.setitem(
        hermes_config._LAST_EXPANDED_CONFIG_BY_PATH, str(home / "config.yaml"), retained
    )
    assert session_config._retained_disagrees(home, doc, frozenset(), False) is True
    assert (
        session_config._retained_disagrees(home, retained, frozenset(), False) is False
    )
    assert (
        session_config._retained_disagrees(tmp_path / "other", doc, frozenset(), False)
        is False
    )


def test_sentinels_absent_from_pagestone_env_key_result(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with owner(tmp_path, {"model": NO_KEY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("OPENROUTER_API_KEY=SENTINEL-ENV-KEY\n")
        raw, _ = _policy_for(home, desc)
        for sentinel in SENTINELS:
            assert sentinel not in raw
