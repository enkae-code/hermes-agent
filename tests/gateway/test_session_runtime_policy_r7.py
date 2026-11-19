"""Round-7 ``runtime_policy`` red tests: complete=true only for the certified shape. The shape is openrouter or
deepseek (exact name), chat-completions api_mode, the canonical base_url (or none), no endpoint override in any
scope, the profile .env's declared key as the only credential source, no secrets block, the local file backend and
an empty fallback chain. Every other profile reports complete false.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway import session_config

CANONICAL = {
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
KEY = {"openrouter": "OPENROUTER_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
MIRROR = "https://mirror.example.test/v1"


def _no_discovery(name, allow_network=True):
    raise AssertionError(f"provider discovery ran for {name!r}")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    from agent import secret_scope
    from hermes_cli import auth, providers

    monkeypatch.setattr(
        secret_scope, "get_secret_str", lambda name, default="": default
    )
    monkeypatch.setattr(auth, "read_credential_pool", lambda provider=None: [])
    monkeypatch.setattr(providers, "get_provider", _no_discovery)
    monkeypatch.setattr(
        session_config,
        "_registries",
        lambda home_doc, source: ({}, True, [], [], True, {}),
    )


def _policy(source, env=(), *, bound=False, chain_known=True):
    return session_config._runtime_policy(
        {},
        source,
        chain=source,
        chain_known=chain_known,
        checkpoint=False,
        secrets=set(),
        bound=bound,
        session=True,
        managed=False,
        known=True,
        followed=False,
        live=None,
        env=frozenset(env),
        named=session_config._named(source),
    )


def _model(provider="openrouter", **extra):
    return {"model": {"provider": provider, "default": "m", **extra}}


@pytest.mark.parametrize("provider", ["openrouter", "deepseek"])
def test_certified_shape_is_complete(provider):
    result = _policy(_model(provider), {KEY[provider]})
    assert result["complete"] is True, result
    assert result["primary"]["credential_kind"] == "api_key", result


@pytest.mark.parametrize(
    "extra",
    [{"api_mode": "chat_completions"}, {"base_url": CANONICAL["openrouter"] + "/"}],
)
def test_absent_or_default_api_mode_and_canonical_base_url_stay_complete(extra):
    assert _policy(_model(**extra), {KEY["openrouter"]})["complete"] is True


@pytest.mark.parametrize(
    "provider", ["OpenRouter", " openrouter", "anthropic", "custom"]
)
def test_provider_name_outside_the_exact_allowlist_is_incomplete(provider):
    result = _policy(_model(provider), {KEY["openrouter"]})
    assert result["complete"] is False, result


@pytest.mark.parametrize("api_mode", ["bedrock_converse", "anthropic_messages", ""])
def test_non_chat_api_mode_is_incomplete(api_mode):
    assert _policy(_model(api_mode=api_mode), {KEY["openrouter"]})["complete"] is False


@pytest.mark.parametrize("base_url", [MIRROR, "https://openrouter.ai/api/v2", ""])
def test_mirror_or_non_canonical_base_url_is_incomplete_even_on_a_builtin_host(
    base_url,
):
    assert _policy(_model(base_url=base_url), {KEY["openrouter"]})["complete"] is False


def test_published_secret_endpoint_override_in_routed_scope_is_incomplete(monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(
        secret_scope,
        "get_secret_str",
        lambda name, default="": MIRROR if name == "DEEPSEEK_BASE_URL" else default,
    )
    assert _policy(_model("deepseek"), {KEY["deepseek"]})["complete"] is False


@pytest.mark.parametrize(
    "var", ["OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "CUSTOM_BASE_URL"]
)
def test_profile_env_endpoint_override_is_incomplete(var):
    assert _policy(_model(), {KEY["openrouter"], var})["complete"] is False


def test_process_env_endpoint_override_is_incomplete(monkeypatch):
    monkeypatch.setenv("CUSTOM_BASE_URL", MIRROR)
    assert _policy(_model(), {KEY["openrouter"]})["complete"] is False


def test_yaml_only_key_is_not_certified(monkeypatch):
    assert _policy(_model(api_key="SENTINEL-KEY"))["complete"] is False
    assert (
        _policy(_model(api_key="SENTINEL-KEY"), {KEY["openrouter"]})["complete"]
        is False
    )


def test_key_read_from_process_env_alone_is_not_certified(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert _policy(_model())["complete"] is False


def test_key_env_pointer_is_not_certified():
    assert (
        _policy(_model(key_env="SOME_KEY"), {KEY["openrouter"], "SOME_KEY"})["complete"]
        is False
    )


def test_numbered_env_key_is_incomplete():
    assert (
        _policy(_model(), {KEY["openrouter"], "OPENROUTER_API_KEY_2"})["complete"]
        is False
    )


def test_session_held_launch_key_is_incomplete():
    assert _policy(_model(), {KEY["openrouter"]}, bound=True)["complete"] is False


@pytest.mark.parametrize(
    "row,complete",
    [
        ({"auth_type": "oauth"}, False),
        ({"auth_type": "api_key", "base_url": MIRROR}, False),
        ({"auth_type": "api_key", "base_url": CANONICAL["openrouter"]}, True),
        ({"auth_type": "api_key"}, True),
    ],
)
def test_pool_rows_must_be_api_key_with_canonical_or_empty_base(
    monkeypatch, row, complete
):
    from hermes_cli import auth

    monkeypatch.setattr(auth, "read_credential_pool", lambda provider=None: [row])
    assert _policy(_model(), {KEY["openrouter"]})["complete"] is complete


def test_secrets_block_is_incomplete():
    assert (
        _policy({**_model(), "secrets": {"api_key": "x"}}, {KEY["openrouter"]})[
            "complete"
        ]
        is False
    )


def test_remote_backend_is_never_read_or_scoped(tmp_path, monkeypatch):
    from gateway import run
    from hermes_cli import config_backend

    def refuse(*args, **kwargs):
        raise AssertionError("remote backend or terminal scope touched")

    class Remote:
        def honors_managed_config(self):
            return False

    monkeypatch.setattr(config_backend, "get_config_backend", lambda: Remote())
    monkeypatch.setattr(config_backend, "read_config_doc_readonly", refuse)
    monkeypatch.setattr(run, "_profile_runtime_scope", refuse)
    conn = SimpleNamespace(
        native_owner=True,
        authority=SimpleNamespace(profile_id=str(tmp_path), runner=None),
    )
    raw = asyncio.run(
        session_config.runtime_policy(conn, SimpleNamespace(session_id=None), {}, None)
    )
    assert raw["runtime_policy"]["complete"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"fallback_providers": [{"provider": "openrouter", "model": "fb"}]},
        {"fallback_model": {"provider": "openrouter", "model": "fb"}},
    ],
)
def test_any_fallback_chain_is_incomplete(extra):
    assert _policy({**_model(), **extra}, {KEY["openrouter"]})["complete"] is False


def test_retained_chain_disagreement_is_incomplete():
    assert (
        _policy(_model(), {KEY["openrouter"]}, chain_known=False)["complete"] is False
    )
