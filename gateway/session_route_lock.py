"""Route lock (``session.create {fallback: "none"}``): the primary route frozen at creation and the check every
turn makes against it. The credential is kept only as a salted HMAC, in the policy's private ``route_lock_json``."""

import hmac
import json
import secrets
from contextlib import contextmanager, suppress
from types import SimpleNamespace

from hermes_state_runtime import RuntimeStoreError

FIELDS = ("provider", "model", "base_url", "api_mode", "credential_kind")


def is_locked(policy):
    return getattr(policy, "route_lock_json", None) is not None


def refusal(detail):
    return RuntimeStoreError("route_lock_mismatch", field="fallback", detail=detail)


def _effective_model(model, provider, base_url):
    """The model the agent will send: ``agent.agent_init``'s normalization, applied to the resolved route."""
    from hermes_cli.anon_auth import pin_model_for_route
    from hermes_cli.model_normalize import (
        _AGGREGATOR_PROVIDERS,
        normalize_model_for_provider,
    )

    with suppress(Exception):
        if provider not in _AGGREGATOR_PROVIDERS:
            model = normalize_model_for_provider(model, provider)
    return pin_model_for_route(provider, base_url, model)


def _route(runtime, model):
    from hermes_cli.route_identity import normalize_route_base_url

    provider, base_url = runtime.get("provider"), runtime.get("base_url")
    return {
        "provider": provider,
        "model": _effective_model(model, provider, base_url),
        "base_url": normalize_route_base_url(base_url),
        "api_mode": runtime.get("api_mode"),
        "credential_kind": runtime.get("credential_kind"),
    }


def _credential_id(salt, key):
    return hmac.new(salt.encode(), (key or "").encode(), "sha256").hexdigest()


def freeze(runtime, model):
    salt = secrets.token_hex(16)
    return json.dumps(
        {
            **_route(runtime, model),
            "salt": salt,
            "credential_id": _credential_id(salt, runtime.get("api_key")),
        },
        sort_keys=True,
    )


def static_key(runtime):
    """True only for a resolved static API key the agent itself sends: an app-server or external-process mode
    authenticates with its own login, whatever label the rung gave the credential."""
    return (
        runtime.get("credential_kind") == "api_key"
        and runtime.get("api_mode") != "codex_app_server"
        and not runtime.get("command")
    )


def verify(policy, runtime, model):
    """Refuse unless the route resolved now is the frozen one, credential included."""
    lock, now = json.loads(policy.route_lock_json), _route(runtime, model)
    if (
        any(lock[name] != now[name] for name in FIELDS)
        or not static_key(runtime)
        or not hmac.compare_digest(
            lock["credential_id"], _credential_id(lock["salt"], runtime.get("api_key"))
        )
    ):
        raise refusal(
            "the route resolved for this turn is not the one the session was locked to"
        )


def verify_final(policy, model, runtime_kwargs):
    """The last word before an agent is built or reused: the (model, runtime) the turn will run, after every
    override (``/model --once``, ``/moa``, ``model_override``, the profile's current default), is the frozen route."""
    if is_locked(policy):
        verify(policy, {**runtime_kwargs, 'credential_kind': 'api_key'}, model)


def refuse_if_locked(policy):
    """Every entry that would send a locked session's content anywhere but its one verified main-turn route
    (side agents, reviews, background work, manual compression, one-shots) calls this before reading anything."""
    if is_locked(policy):
        raise refusal("this command would send the session's content outside its locked route")


def locked_notice(runner, source):
    """The reply a slash handler gives for a locked session's side command, else None."""
    from gateway.session_policy import policy_for_source

    return LOCKED_NOTICE if source is not None and is_locked(policy_for_source(runner, source)) else None


LOCKED_NOTICE = (
    "This session is locked to one route, and that command would send its content elsewhere, so nothing "
    "was sent. (route_lock_mismatch)"
)


def refuse_if_source_locked(runner, source):
    """``refuse_if_locked`` for the slash handlers, which hold a source, not a policy."""
    from gateway.session_policy import policy_for_source

    if source is not None:
        refuse_if_locked(policy_for_source(runner, source))


@contextmanager
def aux_scope(policy):
    """Under a locked turn the auxiliary client may build only the client for the frozen endpoint and
    credential; its recovery never walks a fallback chain or rotates a pool."""
    if not is_locked(policy):
        yield
        return
    from agent.auxiliary_route_lock import locked_route
    from hermes_cli.route_identity import normalize_route_base_url

    lock = json.loads(policy.route_lock_json)

    def allows(client, model):
        key = getattr(client, "api_key", None)
        return (
            isinstance(key, str)
            and normalize_route_base_url(str(getattr(client, "base_url", "")).rstrip("/")) == lock["base_url"].rstrip("/")
            and hmac.compare_digest(lock["credential_id"], _credential_id(lock["salt"], key))
            and _effective_model(model, lock["provider"], lock["base_url"]) == lock["model"]
        )

    with locked_route(allows):
        yield


async def check_revision(authority, ref, policy, expected):
    """A locked session's ``expected_policy_revision`` (checked at submit and again at claim) must be the
    revision ``runtime_policy`` reports now. An unlocked session ignores it: its revision never claims completeness."""
    if expected is None or not is_locked(policy):
        return
    from gateway.session_config import projected_runtime_policy

    current = (await projected_runtime_policy(SimpleNamespace(authority=authority), ref, policy))["runtime_policy"]["revision"]
    if expected != current:
        raise refusal("expected_policy_revision is not the current runtime_policy.revision")


def preview(policy):
    """``(provider, model, base_url)`` of the frozen route: the only route fields a client is shown."""
    lock = json.loads(policy.route_lock_json)
    return lock["provider"], lock["model"], lock["base_url"]


def unsupported_result():
    """The failed, never-run turn for a locked session that would execute in a managed worker."""
    from agent.turn_failure_copy import stamp_failure

    text = (
        "This session is locked to one route, and a managed worker cannot honour that lock, so nothing "
        "was sent. (route_lock_mismatch)"
    )
    return stamp_failure(
        {
            "final_response": text,
            "messages": [],
            "api_calls": 0,
            "tools": [],
            "failed": True,
            "completed": False,
            "error": text,
        },
        "route_lock_mismatch",
        False,
    )
