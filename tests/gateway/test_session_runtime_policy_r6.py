"""Round-6 ``config.get runtime_policy`` red tests: endpoint overrides (CUSTOM_BASE_URL, foreign pool-row base_url),
no secret-source resolver on the getter (and refusal when the profile's sources were never published), registry
callbacks compared by identity (never ``==``), and fallback agreement over declared credentials."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import session_config
from hermes_constants import hermes_home_key
from tests.gateway.test_session_runtime_policy_r4 import _policy_for, _seed_catalog
from tests.gateway.test_session_runtime_policy import policy_owner
from tests.gateway.test_session_runtime_policy_r5 import _samples

OPENROUTER = {
    "provider": "openrouter",
    "default": "primary-model",
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
    assert session_config._chain_key(plain) != session_config._chain_key(declared)
    secret = [{"provider": "openrouter", "model": "m", "api_key": "SENTINEL-KEY"}]
    assert "SENTINEL-KEY" not in json.dumps(
        session_config._chain_key(secret)
    )
