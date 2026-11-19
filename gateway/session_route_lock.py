"""Route lock (``session.create {fallback: "none"}``): the primary route frozen at creation and the check every
turn makes against it. The credential is kept only as a salted HMAC, in the policy's private ``route_lock_json``."""
import hmac
import json
import secrets
from contextlib import suppress

from hermes_state_runtime import RuntimeStoreError

FIELDS = ('provider', 'model', 'base_url', 'api_mode', 'credential_kind')


def is_locked(policy):
    return getattr(policy, 'route_lock_json', None) is not None


def refusal(detail):
    return RuntimeStoreError('route_lock_mismatch', field='fallback', detail=detail)


def _effective_model(model, provider, base_url):
    """The model the agent will send: ``agent.agent_init``'s normalization, applied to the resolved route."""
    from hermes_cli.anon_auth import pin_model_for_route
    from hermes_cli.model_normalize import _AGGREGATOR_PROVIDERS, normalize_model_for_provider
    with suppress(Exception):
        if provider not in _AGGREGATOR_PROVIDERS:
            model = normalize_model_for_provider(model, provider)
    return pin_model_for_route(provider, base_url, model)


def _route(runtime, model):
    from hermes_cli.route_identity import normalize_route_base_url
    provider, base_url = runtime.get('provider'), runtime.get('base_url')
    return {'provider': provider, 'model': _effective_model(model, provider, base_url),
            'base_url': normalize_route_base_url(base_url), 'api_mode': runtime.get('api_mode'),
            'credential_kind': runtime.get('credential_kind')}


def _credential_id(salt, key):
    return hmac.new(salt.encode(), (key or '').encode(), 'sha256').hexdigest()


def freeze(runtime, model):
    salt = secrets.token_hex(16)
    return json.dumps({**_route(runtime, model), 'salt': salt,
                       'credential_id': _credential_id(salt, runtime.get('api_key'))}, sort_keys=True)


def verify(policy, runtime, model):
    """Refuse unless the route resolved now is the frozen one, credential included."""
    lock, now = json.loads(policy.route_lock_json), _route(runtime, model)
    if (any(lock[name] != now[name] for name in FIELDS) or now['credential_kind'] != 'api_key'
            or not hmac.compare_digest(lock['credential_id'], _credential_id(lock['salt'], runtime.get('api_key')))):
        raise refusal('the route resolved for this turn is not the one the session was locked to')


def preview(policy):
    """``(provider, model, base_url)`` of the frozen route: the only route fields a client is shown."""
    lock = json.loads(policy.route_lock_json)
    return lock['provider'], lock['model'], lock['base_url']


def unsupported_result():
    """The failed, never-run turn for a locked session that would execute in a managed worker."""
    from agent.turn_failure_copy import stamp_failure
    text = ('This session is locked to one route, and a managed worker cannot honour that lock, so nothing '
            'was sent. (route_lock_mismatch)')
    return stamp_failure({'final_response': text, 'messages': [], 'api_calls': 0, 'tools': [], 'failed': True,
                          'completed': False, 'error': text}, 'route_lock_mismatch', False)
