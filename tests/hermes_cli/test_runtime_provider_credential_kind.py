from types import SimpleNamespace

from hermes_cli import runtime_provider as rp


def _api_key_rung(monkeypatch, source):
    monkeypatch.setattr(
        rp,
        "_get_model_config",
        lambda: {"provider": "deepseek", "default": "deepseek-v4-pro"},
    )
    monkeypatch.setattr(rp, "load_pool", lambda _provider: None)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_api_key_provider_credentials",
        lambda _provider: {
            "provider": "deepseek",
            "api_key": "sk-test-sentinel",
            "base_url": "https://api.deepseek.com/v1",
            "source": source,
        },
    )
    return rp.resolve_runtime_provider(requested="deepseek")


def test_declared_api_key_env_rung_is_api_key(monkeypatch):
    assert (
        _api_key_rung(monkeypatch, "DEEPSEEK_API_KEY")["credential_kind"] == "api_key"
    )


def test_undeclared_env_source_is_unknown(monkeypatch):
    assert _api_key_rung(monkeypatch, "ANTHROPIC_TOKEN")["credential_kind"] == "unknown"


def test_pool_or_config_source_on_api_key_rung_is_unknown(monkeypatch):
    assert (
        _api_key_rung(monkeypatch, "credential_pool:deepseek")["credential_kind"]
        == "unknown"
    )


def _pool_runtime(monkeypatch, auth_type):
    monkeypatch.setattr(
        rp,
        "_pool_entry_mode_and_url",
        lambda *_a: ("chat_completions", "https://api.deepseek.com/v1"),
    )
    monkeypatch.setattr(rp, "_owned_url_credential", lambda *_a: (False, None))
    entry = SimpleNamespace(
        auth_type=auth_type,
        source="manual",
        base_url="",
        access_token="sk-test-sentinel",
    )
    return rp._resolve_runtime_from_pool_entry(
        provider="deepseek",
        entry=entry,
        requested_provider="deepseek",
        model_cfg={},
        target_model="deepseek-v4-pro",
    )


def test_static_api_key_pool_row_is_api_key(monkeypatch):
    assert _pool_runtime(monkeypatch, "api_key")["credential_kind"] == "api_key"


def test_oauth_pool_row_is_unknown(monkeypatch):
    assert _pool_runtime(monkeypatch, "oauth")["credential_kind"] == "unknown"


def test_anthropic_env_rung_is_unknown(monkeypatch):
    monkeypatch.setattr(
        rp, "_anthropic_token_or_raise", lambda **_kw: "sk-ant-sentinel"
    )
    runtime = rp._anthropic_env_runtime("anthropic", {})
    assert runtime["credential_kind"] == "unknown"


# --- Anthropic OAuth tokens and app-server rewrites are never a static key (route lock) --------------------
import pytest

_OAUTH_TOKENS = ["eyJhbGciOiJSUzI1NiJ9.payload.sig", "cc-oauth-setup-token"]
_ANTHROPIC_ENV = ["ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]


def _anthropic_pool_runtime(monkeypatch, env_var, token):
    monkeypatch.setattr(rp, "_pool_entry_mode_and_url",
                        lambda *_a: ("anthropic_messages", "https://api.anthropic.com"))
    monkeypatch.setattr(rp, "_owned_url_credential", lambda *_a: (False, None))
    entry = SimpleNamespace(auth_type="api_key", source=f"env:{env_var}", base_url="", access_token=token)
    return rp._resolve_runtime_from_pool_entry(
        provider="anthropic", entry=entry, requested_provider="anthropic", model_cfg={}, target_model="claude-x")


@pytest.mark.parametrize("env_var", _ANTHROPIC_ENV)
@pytest.mark.parametrize("token", _OAUTH_TOKENS)
def test_anthropic_oauth_token_in_any_env_pool_row_is_not_a_static_key(monkeypatch, env_var, token):
    assert _anthropic_pool_runtime(monkeypatch, env_var, token)["credential_kind"] == "unknown"


@pytest.mark.parametrize("env_var", ["ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_implicit_oauth_env_var_is_never_a_static_key_whatever_the_token_looks_like(monkeypatch, env_var):
    assert _anthropic_pool_runtime(monkeypatch, env_var, "sk-ant-api03-real")["credential_kind"] == "unknown"


def test_a_console_api_key_in_anthropic_api_key_stays_a_static_key(monkeypatch):
    assert _anthropic_pool_runtime(monkeypatch, "ANTHROPIC_API_KEY", "sk-ant-api03-real")["credential_kind"] == "api_key"


def test_codex_app_server_rewrite_resets_the_credential_kind(monkeypatch):
    monkeypatch.setattr(rp, "_get_model_config",
                        lambda: {"provider": "deepseek", "default": "deepseek-v4-pro", "openai_runtime": "codex_app_server"})
    monkeypatch.setattr(rp, "_maybe_apply_codex_app_server_runtime", lambda **_kw: "codex_app_server")
    monkeypatch.setattr(rp, "load_pool", lambda _provider: None)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_api_key_provider_credentials", lambda _p: {
        "provider": "deepseek", "api_key": "sk-sentinel-0123456789abcdef", "base_url": "https://api.deepseek.com/v1", "source": "DEEPSEEK_API_KEY"})
    runtime = rp.resolve_runtime_provider(requested="deepseek")
    assert (runtime["api_mode"], runtime["credential_kind"]) == ("codex_app_server", "unknown")
