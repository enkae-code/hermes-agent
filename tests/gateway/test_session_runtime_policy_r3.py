"""Round-3 ``config.get runtime_policy``: a session reports what its execution will read."""

import asyncio
import json
import logging
import threading
from pathlib import Path

import hermes_yaml as yaml
import pytest

from gateway import session_config
from hermes_cli.plugins import get_plugin_manager
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket
from tests.gateway.test_session_busy_controls import owner
from tests.gateway.test_session_runtime_policy import (
    LEAKY_URL,
    PRIMARY,
    SENTINELS,
    _read,
)
from tests.gateway.test_session_runtime_policy_r2 import _create, _write


def _create_pagestone(ws):
    # An ordinary session as Pagestone creates it: no profile following, no hooks, no tools.
    return rpc(
        ws,
        "session.create",
        request_id="policy",
        source="tui",
        toolsets=[],
        ignore_rules=True,
        accept_hooks=False,
        follow_profile_config=False,
    )


@pytest.mark.platforms("linux")
def test_ordinary_session_reads_a_fallback_added_after_open(tmp_path):
    fallback = {"provider": "openrouter", "model": "live-fb", "api_key": "SENTINEL-KEY"}
    with owner(tmp_path, {"model": PRIMARY}) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await _create(ws))["result"]["session_id"]
                before = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                config = yaml.safe_load((home / "config.yaml").read_text())
                config["fallback_providers"] = [fallback]
                _write(home, config)
                after = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                assert [row["model"] for row in after["fallback"]["providers"]] == [
                    "live-fb"
                ], after
                assert after["fallback"]["chain_complete"] is True, after
                assert after["revision"] != before["revision"]

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_ordinary_session_checkpoint_follows_a_live_edit(tmp_path):
    with owner(tmp_path, {"model": PRIMARY}) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await _create(ws))["result"]["session_id"]
                config = yaml.safe_load((home / "config.yaml").read_text())
                config["compression"] = {"checkpoint_required": True}
                _write(home, config)
                policy = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                assert policy["compression"] == {"checkpoint_required": True}, policy

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_pagestone_shaped_session_is_complete(tmp_path):
    with owner(tmp_path, {"model": PRIMARY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await _create_pagestone(ws))["result"]["session_id"]
                raw = json.dumps(await _read(ws, session_id=sid))
                policy = json.loads(raw)["result"]["runtime_policy"]
                assert policy["complete"] is True, policy
                assert policy["primary"]["credential_kind"] == "api_key", policy
                assert policy["fallback"] == {
                    "providers": [],
                    "legacy_fallback_model": None,
                    "chain_complete": True,
                }, policy
                assert policy["hooks_active"] == [], policy
                assert policy["outbound_webhooks"]["count"] == 0, policy
                assert policy["compression"] == {"checkpoint_required": False}, policy
                assert "SENTINEL-KEY" not in raw

        asyncio.run(probe())


def test_managed_worker_session_is_incomplete(monkeypatch):
    from gateway import session_managed_worker

    def refuse(policy):
        raise RuntimeStoreError("unsupported_managed_policy")

    monkeypatch.setattr(
        session_managed_worker, "policy_if_managed", lambda policy: policy
    )
    assert session_config._managed_worker(object()) is True
    monkeypatch.setattr(session_managed_worker, "policy_if_managed", refuse)
    assert session_config._managed_worker(object()) is True
    monkeypatch.setattr(
        session_managed_worker, "policy_if_managed", lambda policy: None
    )
    assert session_config._managed_worker(object()) is False


def test_remote_config_backend_is_not_fetched(monkeypatch, tmp_path):
    import hermes_cli.config_backend as backend

    reads = []

    class Remote:
        def honors_managed_config(self):
            return False

        def read_user_doc_readonly(self, home):
            reads.append(home)
            raise AssertionError("remote backend fetched")

    monkeypatch.setattr(backend, "get_config_backend", lambda: Remote())
    assert session_config._home_state(tmp_path) == ({}, False)
    assert reads == []


def test_webhook_secret_env_is_never_resolved_or_logged(monkeypatch, caplog):
    import agent.outbound_webhooks as outbound

    monkeypatch.setenv("SENTINEL_SIGNING_VAR", "SENTINEL-SIGNING-VALUE")

    def parsed(*_args):
        raise AssertionError("webhook config re-parsed")

    monkeypatch.setattr(outbound, "_parse_single_target", parsed)
    doc = {
        "model": PRIMARY,
        "hooks": {
            "outbound": [
                {
                    "url": LEAKY_URL,
                    "events": ["post_tool_call"],
                    "secret_env": "SENTINEL_SIGNING_VAR",
                }
            ]
        },
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
            session=False,
            managed=False,
            known=True,
        )
    assert "SENTINEL-SIGNING-VALUE" not in json.dumps(result)
    assert "SENTINEL-SIGNING-VALUE" not in caplog.text


def test_held_discovery_lock_is_unknown_not_complete(monkeypatch):
    manager = get_plugin_manager()
    monkeypatch.setattr(manager, "_discovered", True)
    monkeypatch.setattr(manager, "_hooks", {})
    held, release = threading.Event(), threading.Event()

    def hold():
        with manager._discovery_lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        assert session_config._observers({}) == ([], False)
    finally:
        release.set()
        holder.join()


def test_memory_provider_fallback_hook_is_not_reported_complete(monkeypatch):
    manager = get_plugin_manager()
    monkeypatch.setattr(manager, "_discovered", True)
    monkeypatch.setattr(manager, "_hooks", {})

    class Context:
        def register_hook(self, name, callback, *, hold_stream=False):
            manager._hooks.setdefault(name, []).append(callback)

    manager._register_fallback_hook(
        Context(), None, "pre_llm_call", lambda **kwargs: None
    )
    assert session_config._observers({}) == ([], False)


def test_sentinels_never_reach_a_pagestone_projection(tmp_path):
    with owner(tmp_path, {"model": PRIMARY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await _create_pagestone(ws))["result"]["session_id"]
                raw = json.dumps(await _read(ws, session_id=sid))
                for sentinel in SENTINELS:
                    assert sentinel not in raw, sentinel

        asyncio.run(probe())
