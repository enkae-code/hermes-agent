"""Round-5 ``config.get runtime_policy``: api_key only for allowlisted static-key routes, the execution ``.env``
tokenizer, the runner's retained chain, route-affecting chain identity, registry object identity and observer labels."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import session_config
from hermes_constants import hermes_home_key
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket
from tests.gateway.test_session_busy_controls import owner
from tests.gateway.test_session_runtime_policy import PRIMARY, _read
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


@pytest.mark.platforms("linux")
def test_subscription_token_in_env_is_not_api_key(tmp_path):
    with owner(tmp_path, {"model": ANTHROPIC, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=SENTINEL-OAT\n")
        raw, _ = _policy_for(home, desc)
        policy = _policy(raw)
        assert policy["primary"]["credential_kind"] == "unknown", policy
        assert policy["complete"] is False, policy
        assert "SENTINEL-OAT" not in raw


@pytest.mark.platforms("linux")
def test_anthropic_api_key_env_alone_is_unknown(tmp_path):
    with owner(tmp_path, {"model": ANTHROPIC, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("ANTHROPIC_API_KEY=SENTINEL-ENV-KEY\n")
        raw, _ = _policy_for(home, desc)
        policy = _policy(raw)
        assert policy["primary"]["credential_kind"] == "unknown", policy
        assert policy["complete"] is False, policy
        assert "SENTINEL-ENV-KEY" not in raw


@pytest.mark.platforms("linux")
def test_azure_entra_route_with_literal_key_is_unknown(tmp_path):
    with owner(tmp_path, {"model": AZURE, "fallback_providers": []}) as (home, _, desc):
        raw, _ = _policy_for(home, desc)
        policy = _policy(raw)
        assert policy["primary"]["credential_kind"] == "unknown", policy
        assert policy["complete"] is False, policy
        assert "SENTINEL-KEY" not in raw


@pytest.mark.platforms("linux")
def test_key_reassigned_empty_in_env_is_not_api_key(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with owner(tmp_path, {"model": NO_KEY, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text(
            "OPENROUTER_API_KEY=SENTINEL-ENV-KEY\nOPENROUTER_API_KEY=\n"
        )
        raw, _ = _policy_for(home, desc)
        policy = _policy(raw)
        assert policy["primary"]["credential_kind"] == "unknown", policy
        assert policy["complete"] is False, policy


def test_unreadable_env_is_unknown_not_absent(tmp_path):
    assert session_config._env_names(tmp_path / ".env") == frozenset()
    (tmp_path / "unreadable").mkdir()
    assert session_config._env_names(tmp_path / "unreadable") is None


def test_non_api_key_pool_row_makes_static_key_route_unknown(monkeypatch):
    # Unit: the daemon regenerates its pool from the .env key at session start, so the row is injected here.
    from hermes_cli import auth

    entry = {"provider": "openrouter", "model": "m", "api_key": "SENTINEL-KEY"}
    monkeypatch.setattr(
        auth,
        "read_credential_pool",
        lambda provider=None: [{"auth_type": "oauth", "source": "hermes_pkce"}],
    )
    assert session_config._route(entry, "m")[0]["credential_kind"] == "unknown"
    monkeypatch.setattr(
        auth,
        "read_credential_pool",
        lambda provider=None: [{"auth_type": "api_key", "source": "env:X"}],
    )
    assert session_config._route(entry, "m")[0]["credential_kind"] == "api_key"


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
    assert agree(runner, tmp_path, [], frozenset(), False) is False
    assert agree(runner, tmp_path, [_entry("old")], frozenset(), False) is True
    assert agree(None, tmp_path, [], frozenset(), False) is False


def test_endpoint_only_fallback_change_is_route_disagreement(tmp_path, monkeypatch):
    _no_pool(monkeypatch)
    key = hermes_home_key(tmp_path)
    runner = SimpleNamespace(
        _fallback_model=[_entry("same")], _fallback_model_by_home={key: [_entry("same")]}
    )
    agree = session_config._runner_chains_agree
    # The unchanged state agrees (the runner-wide slot equals live), so only the endpoint can differ below.
    assert agree(runner, tmp_path, [_entry("same")], frozenset(), False) is True
    moved = [_entry("same", base="https://moved.example.test/v1")]
    assert agree(runner, tmp_path, moved, frozenset(), False) is False


def _samples(monkeypatch, first, second):
    import agent.outbound_webhooks as outbound

    states = iter([first, second])
    monkeypatch.setattr(session_config, "_hook_identity", lambda: next(states))
    monkeypatch.setattr(outbound, "registered_targets", list)
    monkeypatch.setattr(session_config, "_observers", lambda home_doc: ([], True))
    _no_pool(monkeypatch)
    doc = {"model": PRIMARY}
    return session_config._runtime_policy(
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
def test_pagestone_shape_declares_editor_mcp_and_stays_complete(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with owner(tmp_path, {"model": NO_KEY, "fallback_providers": []}) as (
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
        assert policy["complete"] is True, policy
