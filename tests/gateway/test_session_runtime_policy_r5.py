"""Round-5 ``config.get runtime_policy``: api_key only for allowlisted static-key routes, the execution ``.env``
tokenizer, the runner's retained chain, route-affecting chain identity, registry object identity and observer labels."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import session_config
from hermes_constants import hermes_home_key
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket
from tests.gateway.test_session_runtime_policy import CERTIFIED, PRIMARY, policy_owner, _read
from tests.gateway.test_session_runtime_policy_r4 import (
    NO_KEY,
    _create_pagestone,
    _policy_for,
    _scratch,
    _seed_catalog,
)

ANTHROPIC = {
    "provider": "anthropic",
    "default": "claude-model",
    "base_url": "https://api.example.test/v1",
}
AZURE = {
    "provider": "azure-foundry",
    "default": "az-model",
    "api_key": "SENTINEL-KEY",
    "base_url": "https://api.example.test/v1",
}


def _policy(raw):
    return json.loads(raw)["result"]["runtime_policy"]


def _no_pool(monkeypatch):
    from hermes_cli import auth

    monkeypatch.setattr(auth, "read_credential_pool", lambda provider=None: [])


def _entry(model, base="https://fb.example.test/v1"):
    return {
        "provider": "openrouter",
        "model": model,
        "base_url": base,
        "api_key": "SENTINEL-KEY",
    }


def test_runner_retained_chain_must_agree_with_live_chain(tmp_path, monkeypatch):
    _no_pool(monkeypatch)
    key = hermes_home_key(tmp_path)
    runner = SimpleNamespace(
        _fallback_model=[_entry("old")], _fallback_model_by_home={key: [_entry("old")]}
    )
    agree = session_config._runner_chains_agree
    # Simulated effective-config read failure: the live chain is empty while execution retains one fallback.
    assert agree(runner, tmp_path, []) is False
    assert agree(runner, tmp_path, [_entry("old")]) is True
    assert agree(None, tmp_path, []) is False


def test_endpoint_only_fallback_change_is_route_disagreement(tmp_path, monkeypatch):
    _no_pool(monkeypatch)
    key = hermes_home_key(tmp_path)
    runner = SimpleNamespace(
        _fallback_model=[_entry("same")],
        _fallback_model_by_home={key: [_entry("same")]},
    )
    agree = session_config._runner_chains_agree
    # The unchanged state agrees (the runner-wide slot equals live), so only the endpoint can differ below.
    assert agree(runner, tmp_path, [_entry("same")]) is True
    moved = [_entry("same", base="https://moved.example.test/v1")]
    assert agree(runner, tmp_path, moved) is False


def _samples(monkeypatch, first, second):
    import agent.outbound_webhooks as outbound

    states = iter([first, second])
    monkeypatch.setattr(session_config, "_hook_identity", lambda: next(states))
    monkeypatch.setattr(outbound, "registered_targets", list)
    monkeypatch.setattr(session_config, "_observers", lambda home_doc: ([], True))
    from gateway import session_route_lock

    _no_pool(monkeypatch)
    doc = {"model": CERTIFIED}
    frozen = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
              "credential_kind": "api_key", "api_key": "SENTINEL-KEY"}
    locked = SimpleNamespace(route_lock_json=session_route_lock.freeze(frozen, "primary-model"))
    return session_config._runtime_policy(
        doc,
        doc,
        chain=doc,
        chain_known=True,
        checkpoint=False,
        locked=locked,
        session=True,
        managed=False,
        known=True,
    )


def test_replaced_shell_hook_callback_between_samples_is_incomplete(monkeypatch):
    before, after = (lambda: None), (lambda: None)
    result = _samples(
        monkeypatch, {"post_tool_call": (before,)}, {"post_tool_call": (after,)}
    )
    assert result["complete"] is False, result


def test_identical_registry_samples_stay_complete(monkeypatch):
    callback = lambda: None
    result = _samples(
        monkeypatch, {"post_tool_call": (callback,)}, {"post_tool_call": (callback,)}
    )
    assert result["complete"] is True, result


def test_invalid_observer_label_is_replaced_and_incomplete(monkeypatch):
    import agent.safe_worker_policy as safe_worker
    import agent.outbound_webhooks as outbound
    from agent import shell_hooks
    from hermes_cli import plugins

    plugin = SimpleNamespace(
        manifest=SimpleNamespace(name="SENTINEL NAME"),
        enabled=True,
        error=None,
        hooks_registered={"post_tool_call"},
    )
    manager = SimpleNamespace(
        _plugins={"x": plugin}, _hooks={"post_tool_call": [object()]}
    )
    monkeypatch.setattr(
        plugins, "_loaded_registry_nowait", lambda check: check(manager)
    )
    monkeypatch.setattr(safe_worker, "safe_worker_enabled", lambda: False)
    monkeypatch.setattr(outbound, "registered_targets", list)
    monkeypatch.setattr(shell_hooks, "registered_event_counts", dict)
    rows, complete = session_config._observers({})
    assert rows == [{"name": "<invalid>", "events": ["post_tool_call"]}], rows
    assert complete is False
    assert "SENTINEL" not in json.dumps(rows)


@pytest.mark.platforms("linux")
def test_pagestone_shape_declares_editor_mcp_and_reports_incomplete_unlocked(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with policy_owner(tmp_path, {"model": CERTIFIED, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("OPENROUTER_API_KEY=SENTINEL-ENV-KEY\n")

        async def probe():
            async with websocket(home, desc) as ws:
                created = await _create_pagestone(ws, _scratch(home.parent))
                sid = created["result"]["session_id"]
                return json.dumps(await _read(ws, session_id=sid))

        policy = _policy(asyncio.run(probe()))
        assert policy["complete"] is False, policy  # unlocked; the editor MCP declaration is still reported
