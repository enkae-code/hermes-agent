"""``config.get runtime_policy``: the native owner connection reads a non-secret effective route projection."""

import asyncio
import json

import hermes_yaml as yaml
import pytest
from websockets.asyncio.client import connect

from tests.gateway.test_session_busy_controls import owner
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket

PRIMARY = {
    "provider": "openrouter",
    "default": "primary-model",
    "api_key": "SENTINEL-KEY",
    "base_url": "https://api.example.test/v1",
}
LEAKY_URL = "https://SENTINEL-USER@api.example.test/SENTINEL-PATH?token=SENTINEL-QUERY"
SENTINELS = (
    "SENTINEL-KEY",
    "SENTINEL-USER",
    "SENTINEL-PATH",
    "SENTINEL-QUERY",
    "SENTINEL-HEADER",
    "SENTINEL-CMD",
)


# The certified shape (round 7): the key is the profile .env's, the endpoint is the built-in one, no YAML key.
CERTIFIED = {"provider": "openrouter", "default": "primary-model"}


def policy_owner(tmp_path, config=None):
    # S4: the harness's loopback OPENAI_BASE_URL is an endpoint override; the projection must see it absent.
    return owner(tmp_path, config, extra_env={"OPENAI_BASE_URL": ""})


def _read(ws, **params):
    return rpc(ws, "config.get", key="runtime_policy", **params)


@pytest.mark.platforms("linux")
def test_api_key_primary_with_empty_fallback_is_complete(tmp_path):
    with policy_owner(tmp_path, {"model": CERTIFIED, "fallback_providers": []}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("OPENROUTER_API_KEY=SENTINEL-ENV-KEY\n")

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (
                    await rpc(
                        ws,
                        "session.create",
                        request_id="policy",
                        source="tui",
                        toolsets=[],
                    )
                )["result"]["session_id"]
                policy = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                assert policy["complete"] is True, policy
                assert (await _read(ws))["result"]["runtime_policy"][
                    "complete"
                ] is False
                assert policy["primary"]["provider"] == "openrouter"
                assert policy["primary"]["model"] == "primary-model"
                assert policy["primary"]["credential_kind"] == "api_key"
                assert policy["primary"]["endpoint_host"]
                assert policy["fallback"] == {
                    "providers": [],
                    "legacy_fallback_model": None,
                    "chain_complete": True,
                }

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_primary_without_an_explicit_key_reference_is_unknown_and_incomplete(tmp_path):
    with policy_owner(
        tmp_path, {"model": {"provider": "openrouter", "default": "primary-model"}}
    ) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                policy = (await _read(ws))["result"]["runtime_policy"]
                assert policy["primary"]["credential_kind"] == "unknown", policy
                assert policy["complete"] is False

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_fallback_providers_and_legacy_fallback_model_are_both_reported(tmp_path):
    fallback = {"provider": "openrouter", "api_key": "SENTINEL-KEY"}
    config = {
        "model": PRIMARY,
        "fallback_providers": [{**fallback, "model": "listed-fallback"}],
        "fallback_model": {**fallback, "model": "legacy-fallback"},
    }
    with policy_owner(tmp_path, config) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                fallback_view = (await _read(ws))["result"]["runtime_policy"][
                    "fallback"
                ]
                assert [row["model"] for row in fallback_view["providers"]] == [
                    "listed-fallback",
                    "legacy-fallback",
                ]
                assert fallback_view["providers"][0]["credential_kind"] == "unknown"  # S5: a YAML key is not certified
                assert fallback_view["legacy_fallback_model"] == "legacy-fallback"
                assert fallback_view["chain_complete"] is False  # S7: any fallback chain is incomplete

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_no_credential_url_path_query_or_header_reaches_the_projection(tmp_path):
    model = {**PRIMARY, "base_url": LEAKY_URL, "headers": {"X-Api": "SENTINEL-HEADER"}}
    config = {
        "model": model,
        "fallback_providers": [
            {
                "provider": "openrouter",
                "model": "fb",
                "api_key": "SENTINEL-KEY",
                "base_url": LEAKY_URL,
            }
        ],
        "hooks": {
            "post_tool_call": [{"command": "echo SENTINEL-CMD"}],
            "outbound": [{"url": LEAKY_URL, "events": ["post_tool_call"]}],
        },
        "hooks_auto_accept": True,
    }
    with policy_owner(tmp_path, config) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (
                    await rpc(
                        ws,
                        "session.create",
                        request_id="policy",
                        source="tui",
                        toolsets=[],
                    )
                )["result"]["session_id"]
                raw = json.dumps(await _read(ws, session_id=sid))
                assert "api.example.test" in raw
                for sentinel in SENTINELS:
                    assert sentinel not in raw, sentinel

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_active_hooks_webhooks_and_observers_are_reported(tmp_path):
    config = {
        "model": PRIMARY,
        "hooks": {
            "post_tool_call": [{"command": "echo SENTINEL-CMD"}],
            "outbound": [{"url": LEAKY_URL, "events": ["post_tool_call"]}],
        },
        "hooks_auto_accept": True,
    }
    with policy_owner(tmp_path, config) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (
                    await rpc(
                        ws,
                        "session.create",
                        request_id="policy",
                        source="tui",
                        toolsets=[],
                    )
                )["result"]["session_id"]
                policy = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                assert policy["hooks_active"] == ["post_tool_call"], policy
                assert policy["outbound_webhooks"] == {
                    "count": 1,
                    "events": ["post_tool_call"],
                    "hosts": ["api.example.test"],
                }, policy
                assert isinstance(policy["observer_plugins"], list)
                assert all(
                    {"name", "events"} <= row.keys()
                    for row in policy["observer_plugins"]
                )

        asyncio.run(probe())


@pytest.mark.platforms("linux")
@pytest.mark.parametrize(
    "compression, expected",
    [
        ({"checkpoint_required": True}, True),
        ({"checkpoint_required": False}, False),
        ({}, False),
        ({"checkpoint_required": "maybe"}, None),
    ],
)
def test_checkpoint_required_is_projected_and_unknown_is_incomplete(
    tmp_path, compression, expected
):
    with policy_owner(tmp_path, {"model": CERTIFIED, "compression": compression}) as (
        home,
        _,
        desc,
    ):
        (home / ".env").write_text("OPENROUTER_API_KEY=SENTINEL-ENV-KEY\n")

        async def probe():
            async with websocket(home, desc) as ws:
                sid = (
                    await rpc(
                        ws,
                        "session.create",
                        request_id="policy",
                        source="tui",
                        toolsets=[],
                    )
                )["result"]["session_id"]
                policy = (await _read(ws, session_id=sid))["result"]["runtime_policy"]
                assert policy["compression"] == {"checkpoint_required": expected}, (
                    policy
                )
                assert policy["complete"] is (expected is not None)

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_non_native_owner_connection_is_refused(tmp_path):
    with policy_owner(tmp_path, {"model": PRIMARY}) as (_, _, desc):

        async def probe():
            other_url = (
                desc["api_origin"].replace("http:", "ws:")
                + "/api/ws?token=other-controls-actor"
            )
            async with connect(other_url) as other:
                assert (await _read(other))["error"]["message"] == "permission_denied"

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_revision_is_stable_until_a_projected_field_changes(tmp_path):
    with policy_owner(tmp_path, {"model": PRIMARY}) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                first = (await _read(ws))["result"]["runtime_policy"]["revision"]
                assert (await _read(ws))["result"]["runtime_policy"][
                    "revision"
                ] == first
                config = yaml.safe_load((home / "config.yaml").read_text())
                config["fallback_providers"] = [
                    {"provider": "openrouter", "model": "fb", "api_key": "SENTINEL-KEY"}
                ]
                (home / "config.yaml").write_text(yaml.safe_dump(config))
                assert (await _read(ws))["result"]["runtime_policy"][
                    "revision"
                ] != first

        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_runtime_describe_advertises_the_capability(tmp_path):
    with policy_owner(tmp_path, {"model": PRIMARY}) as (home, _, desc):

        async def probe():
            async with websocket(home, desc) as ws:
                capabilities = (await rpc(ws, "runtime.describe"))["result"][
                    "capabilities"
                ]
                assert "runtime-policy-v1" in capabilities

        asyncio.run(probe())
