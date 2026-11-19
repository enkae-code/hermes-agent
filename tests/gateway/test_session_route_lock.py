"""Route lock (``session.create {fallback: "none"}``): creation refuses any primary route whose
selected credential is not a static API key, judged by the resolver that execution uses."""

from types import SimpleNamespace

import pytest

from gateway import session_local
from gateway.session_policy import build_policy
from hermes_state_runtime import RuntimeStoreError

_CFG = {"platform_toolsets": {"cli": ["terminal"]}}


def test_fallback_none_is_the_only_accepted_fallback_value(tmp_path):
    with pytest.raises(RuntimeStoreError) as caught:
        build_policy({"cwd": str(tmp_path), "fallback": "any"}, dict(_CFG))
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "fallback")
    build_policy({"cwd": str(tmp_path), "fallback": "none"}, dict(_CFG))


@pytest.mark.parametrize(
    "kind, refused", [("api_key", False), ("unknown", True), ("oauth", True)]
)
def test_create_refuses_unless_the_resolver_selected_a_static_key(
    monkeypatch, kind, refused
):
    seen = {}

    def fake_route_runtime(policy, frozen, model, key, *, fallback=True):
        seen.update(key=key, fallback=fallback)
        return {"credential_kind": kind, "provider": "openrouter"}, None

    import gateway.session_worker_construct as worker

    monkeypatch.setattr(worker, "route_runtime", fake_route_runtime)
    policy = SimpleNamespace(
        effective=lambda authority=None: ({"model": {"provider": "openrouter"}}, "m")
    )
    import gateway.session_policy as sp

    monkeypatch.setattr(
        sp, "route_launch_key", lambda authority, policy, frozen: "LAUNCH-KEY"
    )

    if not refused:
        session_local._refuse_unless_static_key(None, policy)
        assert seen == {"key": "LAUNCH-KEY", "fallback": False}
        return
    with pytest.raises(RuntimeStoreError) as caught:
        session_local._refuse_unless_static_key(None, policy)
    assert (caught.value.reason, caught.value.field) == ("invalid_params", "fallback")
    assert seen["fallback"] is False
