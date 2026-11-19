"""Round-6 ``config.get runtime_policy`` red tests: endpoint overrides (CUSTOM_BASE_URL, foreign pool-row base_url),
no secret-source resolver on the getter (and refusal when the profile's sources were never published), registry
callbacks compared by identity (never ``==``), and fallback agreement over declared credentials."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import session_config
from hermes_constants import hermes_home_key
from tests.gateway.test_session_busy_controls import owner
from tests.gateway.test_session_runtime_policy_r4 import _policy_for, _seed_catalog
from tests.gateway.test_session_runtime_policy_r5 import _samples

OPENROUTER = {
    "provider": "openrouter",
    "default": "primary-model",
    "api_key": "SENTINEL-KEY",
}


def _policy(raw):
    return json.loads(raw)["result"]["runtime_policy"]


def _no_pool(monkeypatch):
    from hermes_cli import auth

    monkeypatch.setattr(auth, "read_credential_pool", lambda provider=None: [])


def _catalog_stub(monkeypatch):
    # The models.dev catalog is absent in-process; its ``api`` field is the built-in endpoint the projection reads.
    from hermes_cli import providers

    defs = {
        "openrouter": SimpleNamespace(
            base_url="https://openrouter.ai/api/v1",
            base_url_env_var="OPENROUTER_BASE_URL",
            api_key_env_vars=("OPENROUTER_API_KEY",),
        ),
        "deepseek": SimpleNamespace(
            base_url="https://api.deepseek.com/v1",
            base_url_env_var="DEEPSEEK_BASE_URL",
            api_key_env_vars=("DEEPSEEK_API_KEY",),
        ),
    }
    monkeypatch.setattr(
        providers, "get_provider", lambda name, allow_network=True: defs.get(name)
    )


@pytest.mark.platforms("linux")
def test_openrouter_host_is_canonical_without_endpoint_override(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with owner(tmp_path, {"model": OPENROUTER, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        policy = _policy(_policy_for(home, desc)[0])
        assert policy["primary"]["endpoint_host"] == "openrouter.ai", policy


@pytest.mark.platforms("linux")
def test_profile_env_custom_base_url_overrides_openrouter_host(tmp_path, monkeypatch):
    _seed_catalog(monkeypatch)
    with owner(tmp_path, {"model": OPENROUTER, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("CUSTOM_BASE_URL=https://evil.example.test/v1\n")
        raw, _ = _policy_for(home, desc)
        policy = _policy(raw)
        assert policy["primary"]["endpoint_host"] == "", policy
        assert policy["complete"] is False, policy
        assert "evil.example.test" not in raw


def test_custom_base_url_in_process_env_makes_static_route_host_unknown(monkeypatch):
    _no_pool(monkeypatch)
    _catalog_stub(monkeypatch)
    entry = {"provider": "openrouter", "model": "m", "api_key": "SENTINEL-KEY"}
    assert session_config._host(session_config._route(entry, "m")[1]) == "openrouter.ai"
    assert (
        session_config._route(entry, "m", env=frozenset({"CUSTOM_BASE_URL"}))[1] == ""
    )
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://evil.example.test/v1")
    assert session_config._route(entry, "m")[1] == ""


def test_foreign_pool_row_base_url_makes_static_route_host_unknown(monkeypatch):
    from hermes_cli import auth

    _catalog_stub(monkeypatch)
    entry = {"provider": "deepseek", "model": "m", "api_key": "SENTINEL-KEY"}
    monkeypatch.setattr(
        auth,
        "read_credential_pool",
        lambda provider=None: [
            {"auth_type": "api_key", "base_url": "https://proxy.evil.example.test/v1"}
        ],
    )
    identity, base = session_config._route(entry, "m")
    assert identity["credential_kind"] == "api_key", identity
    assert base == "", base


def test_canonical_or_empty_pool_row_base_url_keeps_host(monkeypatch):
    from hermes_cli import auth

    _catalog_stub(monkeypatch)
    entry = {"provider": "deepseek", "model": "m", "api_key": "SENTINEL-KEY"}
    for row in (
        {"auth_type": "api_key", "base_url": ""},
        {"auth_type": "api_key", "base_url": "https://api.deepseek.com/v1"},
    ):
        monkeypatch.setattr(
            auth, "read_credential_pool", lambda provider=None, row=row: [row]
        )
        assert (
            session_config._host(session_config._route(entry, "m")[1])
            == "api.deepseek.com"
        )


def test_getter_never_invokes_secret_source_resolvers(tmp_path, monkeypatch):
    from agent.secret_sources import command
    from hermes_cli import env_loader

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        json.dumps({"model": OPENROUTER, "fallback_providers": []})
    )
    calls = []

    def flag(*args, **kwargs):
        calls.append(args)
        raise AssertionError(
            "secret source resolver invoked by config.get runtime_policy"
        )

    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", flag)
    monkeypatch.setattr(env_loader, "_hydrate_profile_secret_sources", flag)
    monkeypatch.setattr(command, "_run_helper", flag)
    connection = SimpleNamespace(
        native_owner=True,
        authority=SimpleNamespace(
            profile_id=str(home), agent=lambda ref: None, runner=None
        ),
    )
    result = asyncio.run(
        session_config.runtime_policy(
            connection, SimpleNamespace(session_id=None), {}, None
        )
    )
    assert calls == []
    assert result["runtime_policy"]["complete"] is False


def test_unpublished_secret_sources_are_unknown(tmp_path, monkeypatch):
    from hermes_cli import env_loader

    monkeypatch.setattr(env_loader, "_APPLIED_HOMES", set())
    assert session_config._sources_unpublished(tmp_path, {"secrets": {"x": 1}}) is True
    assert session_config._sources_unpublished(tmp_path, {}) is False
    monkeypatch.setattr(env_loader, "_APPLIED_HOMES", {hermes_home_key(tmp_path)})
    assert session_config._sources_unpublished(tmp_path, {"secrets": {"x": 1}}) is False


class _EqualToAnything:
    def __call__(self):
        return None

    def __eq__(self, other):
        return True

    __hash__ = object.__hash__


def test_replaced_hook_with_equal_callable_between_samples_is_incomplete(monkeypatch):
    result = _samples(
        monkeypatch,
        {"post_tool_call": (_EqualToAnything(),)},
        {"post_tool_call": (_EqualToAnything(),)},
    )
    assert result["complete"] is False, result


def test_unavailable_identity_sample_is_incomplete(monkeypatch):
    assert _samples(monkeypatch, False, False)["complete"] is False


def test_declared_credential_in_fallback_entry_changes_chain_identity(monkeypatch):
    _no_pool(monkeypatch)
    _catalog_stub(monkeypatch)
    plain = [{"provider": "openrouter", "model": "m"}]
    declared = [
        {"provider": "openrouter", "model": "m", "key_env": "OPENROUTER_API_KEY"}
    ]
    assert session_config._chain_key(
        plain, frozenset(), False
    ) != session_config._chain_key(declared, frozenset(), False)
    secret = [{"provider": "openrouter", "model": "m", "api_key": "SENTINEL-KEY"}]
    assert "SENTINEL-KEY" not in json.dumps(
        session_config._chain_key(secret, frozenset(), False)
    )
