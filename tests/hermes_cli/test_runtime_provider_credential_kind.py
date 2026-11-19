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
