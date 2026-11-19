"""Owner-bound native settings and model inventory, never legacy execution."""
import asyncio
from functools import partial
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {'config.get': partial(config_get, connection),
            'model.options': partial(model_options, connection)}


def _authorize(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if 'session:read' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    for name in ('profile', 'session_id'):
        if name in params and not isinstance(params[name], str):
            raise RuntimeStoreError('invalid_params')
    if params.get('profile'):
        from hermes_cli.profiles import profile_matches_home
        if not profile_matches_home(params['profile'], Path(authority.profile_id)):
            raise RuntimeStoreError('profile_mismatch')
    if ref.session_id:
        authority.authorize(actor, ref, 'session:read')
        from gateway.session_policy import policy_for_source
        return policy_for_source(authority.runner, authority.sessions[ref.session_id].source)
    return None


def mcp_revision(policy, cfg) -> str:
    """``mcp_rev``: the MCP-relevant sections a client polls (``config.get mtime``) and names back on
    ``reload.mcp``. Read from the session's frozen policy when it has one — a live config edit is not an
    MCP reload request, it reaches a chat as a committed generation — so the value a client is told to load
    is the value the session already holds."""
    return hashlib.sha256(json.dumps({k: (policy.config() if policy else cfg).get(k)
                                      for k in ('mcp', 'mcp_servers', 'tools')},
                                     sort_keys=True).encode()).hexdigest()[:12]


def _client_config(cfg):
    # Native full-config consumers are presentation-only. Do not export provider,
    # MCP, plugin or terminal configuration (which can contain arbitrary secrets).
    from hermes_cli.config import redact_config_value
    result = redact_config_value({key: cfg[key] for key in (
        'display', 'approvals', 'paste_collapse_threshold', 'paste_collapse_char_threshold') if key in cfg})
    if 'display' in result:
        # Its readers are local surfaces (Ink, Desktop): the local key a local /reasoning show|hide or
        # /footer saved wins over the global one (T7).
        from gateway.display_config import local_display_config
        result['display'] = local_display_config(result['display'])
    # Allowlisted after redaction: ``record_key`` is a keybinding, not a credential, but the
    # structural masker treats every ``*_key`` leaf as one. The voice section's real secrets
    # (``api_key``) never enter the projection.
    result['voice'] = {key: cfg.get('voice', {}).get(key) for key in ('record_key', 'submit_mode')
                       if key in cfg.get('voice', {})}
    return result


async def config_get(connection, ref, params):
    if set(params) - {'session_id', 'profile', 'key', 'cwd'}:
        raise RuntimeStoreError('invalid_params')
    key = params.get('key')
    if not isinstance(key, str):
        raise RuntimeStoreError('invalid_params')
    if key in ('busy', 'verbose', 'yolo', 'show_reasoning'):
        from gateway.session_busy_controls import busy_config
        return await busy_config(connection, ref, params)
    policy = _authorize(connection, ref, params)
    if 'cwd' in params and (key != 'project' or not isinstance(params['cwd'], str)):
        raise RuntimeStoreError('invalid_params')
    if key == 'voice':
        return _voice(connection, ref)
    if key == 'runtime_policy':
        return await runtime_policy(connection, ref, params)
    agent = connection.authority.agent(ref) if ref.session_id else None

    def read():
        from gateway.run import _profile_runtime_scope
        from hermes_cli.config import load_config
        from hermes_cli.config_backend import config_version_or_none, version_token
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_constants import DEFAULT_INDICATOR_STYLE
        from tools.approval_context import _get_approval_mode
        home = Path(connection.authority.profile_id)
        with _profile_runtime_scope(home):
            cfg = load_config()
            display = cfg.get('display') or {}
            getters = {
                # The user's own settings, not DEFAULT_CONFIG: clients read an absent key as their
                # own default (the TUI streams unless display.streaming is explicitly false).
                'full': lambda: {'config': _client_config(load_user_config_effective(home / 'config.yaml'))},
                'mtime': lambda: {'mtime': version_token(config_version_or_none(home / 'config.yaml')),
                                 # Frozen tool policy is not a live MCP reload request.
                                 'mcp_rev': mcp_revision(policy, cfg)},
                'reasoning': lambda: _reasoning(agent, policy, cfg, connection, ref),
                'fast': lambda: _fast(connection, ref, policy, agent, cfg),
                'service_tier': lambda: _service_tier(connection, ref, policy),
                'daybreak': lambda: {'value': (cfg.get('agent') or {}).get('daybreak') is True},
                'skin': lambda: {'value': display.get('skin', 'default')},
                'theme': lambda: {'value': display.get('tui_theme', 'auto')},
                'indicator': lambda: {'value': display.get('tui_status_indicator', DEFAULT_INDICATOR_STYLE)},
                'details_mode': lambda: {'value': display.get('details_mode', 'collapsed')},
                'approvals.mode': lambda: {'value': _get_approval_mode()},
                'approval_mode': lambda: {'value': _get_approval_mode()},
                'project': lambda: _project(params, policy, cfg),
                'profile': lambda: {'home': str(home), 'display': str(home)},
            }
            if key not in getters:
                raise RuntimeStoreError('invalid_params')
            return getters[key]()
    return await asyncio.to_thread(read)


def session_voice_mode(connection, ref):
    """``off`` / ``voice_only`` / ``all``: the session's ``/voice`` mode as the messaging handler stored it."""
    runner, live = connection.authority.runner, connection.authority.sessions[ref.session_id]
    if live.source is None:  # a history mount: no chat for a voice mode to belong to
        return 'off'
    modes = getattr(runner, '_voice_mode', None) or {}
    return modes.get(runner._voice_key_for_source(live.source), 'off')


def local_voice_enabled(session_id):
    """True/False for a local session the running owner holds (its gateway ``/voice`` mode), else
    None (no owner in this process, or not one of its sessions: the sidecar's env flag applies)."""
    from gateway.run_api import _ACTIVE_RUNNER
    from gateway.session_authorities import all_authorities
    if _ACTIVE_RUNNER is None or not isinstance(session_id, str) or not session_id:
        return None
    for authority in all_authorities(_ACTIVE_RUNNER):
        live = getattr(authority, 'sessions', {}).get(session_id)
        if live is not None:
            if live.source is None:  # a history mount: no chat for a voice mode to belong to
                return False
            modes = getattr(_ACTIVE_RUNNER, '_voice_mode', None) or {}
            return modes.get(_ACTIVE_RUNNER._voice_key_for_source(live.source), 'off') != 'off'
    return None


def set_local_voice_mode(session_id, mode):
    """The one writer for a local session's voice mode outside ``/voice`` (wake, spoken stop): sets
    and persists the owner's per-session mode. True when the running owner holds the session, else
    None (the sidecar's env flag applies)."""
    from gateway.run_api import _ACTIVE_RUNNER
    from gateway.session_authorities import all_authorities
    if _ACTIVE_RUNNER is None or not isinstance(session_id, str) or not session_id:
        return None
    for authority in all_authorities(_ACTIVE_RUNNER):
        live = getattr(authority, 'sessions', {}).get(session_id)
        if live is not None:
            _ACTIVE_RUNNER._voice_mode[_ACTIVE_RUNNER._voice_key_for_source(live.source)] = mode
            _ACTIVE_RUNNER._save_voice_modes()
            local_voice_transition(live.source, mode)
            return True
    return None


def local_voice_transition(source, mode):
    """A local session's voice mode went ``off``: end the push-to-talk capture it started in this
    process (and drop its late callbacks), as the sidecar's ``voice.toggle off`` did. Another
    session's capture is left alone."""
    import sys
    from gateway.config import Platform
    server = sys.modules.get('tui_gateway.server')
    if mode == 'off' and server is not None and getattr(source, 'platform', None) == Platform.LOCAL:
        server._stop_session_voice_capture(source.chat_id)


def _voice(connection, ref):
    """The session's own ``/voice`` mode, so Ink mirrors the gateway's per-session state instead of
    the sidecar's process-global ``HERMES_VOICE`` (T9)."""
    if not ref.session_id:
        raise RuntimeStoreError('invalid_params')
    return {'value': session_voice_mode(connection, ref)}


def _fast(connection, ref, policy, agent, cfg):
    from agent.fast_mode import parse_service_tier
    if ref.session_id:  # the session's next-turn tier: its /fast pin, else its policy's
        from gateway.session_tui_config import current_tier
        tier = current_tier(connection, ref, policy)
    else:
        tier = getattr(agent, 'service_tier', None) or parse_service_tier(cfg.get('agent', {}).get('service_tier'))
    return {'value': {'priority': 'fast', 'ultrafast': 'ultrafast'}.get(tier, 'normal')}


def _service_tier(connection, ref, policy):
    from agent.fast_mode import service_tier_word
    from gateway.session_tui_config import current_tier, service_tier_options
    if not ref.session_id:
        raise RuntimeStoreError('invalid_params')
    return {'value': service_tier_word(current_tier(connection, ref, policy)),
            'options': service_tier_options(connection, ref, policy)}


def _reasoning(agent, policy, cfg, connection=None, ref=None):
    from hermes_constants import resolve_reasoning_config
    value = getattr(agent, 'reasoning_config', None)
    if not isinstance(value, dict) and policy is not None:
        # The next turn's level: a typed session /reasoning, else the frozen (or followed) one.
        runner = connection.authority.runner
        value = policy.current_reasoning_config(runner, connection.authority.sessions[ref.session_id].route)
    elif not isinstance(value, dict):
        value = resolve_reasoning_config(cfg, '')
    effort = 'none' if value and value.get('enabled') is False else (value or {}).get('effort', 'medium')
    from gateway.display_config import local_display_config
    shown = local_display_config(cfg.get('display')).get('show_reasoning', True)
    return {'value': effort, 'display': 'show' if shown else 'hide'}


def _project(params, policy, cfg):
    from tui_gateway import git_probe
    cwd = params.get('cwd') or (policy.cwd if policy else cfg.get('terminal', {}).get('cwd'))
    if not cwd:
        from agent.runtime_cwd import resolve_agent_cwd
        cwd = str(resolve_agent_cwd())
    return {'cwd': cwd, 'branch': git_probe.branch(cwd)}


_CREDENTIAL_KINDS = {'api_key': 'api_key', 'oauth_device_code': 'oauth', 'oauth_external': 'oauth',
                     'external_process': 'cli'}
_KEY_REFERENCES = ('api_key', 'api_key_env', 'key_env')


def _route(entry, model):
    """``(identity, base_url)`` for one route. An api_key provider is ``api_key`` only when the route
    names its key itself; an env-only key is not verified here, so it reads ``unknown``. Nothing from
    the key, the URL path, query or headers is returned."""
    from hermes_cli.providers import get_provider
    provider = str(entry.get('provider') or '').strip()
    pdef = get_provider(provider, allow_network=False) if provider else None
    kind = _CREDENTIAL_KINDS.get(pdef.auth_type) if pdef else None
    if kind == 'api_key' and not any(entry.get(k) for k in _KEY_REFERENCES):
        kind = None
    base = entry.get('base_url') or (pdef.base_url if pdef else '')
    return {'provider': provider, 'model': model, 'credential_kind': kind or 'unknown'}, base


def _fallback(source):
    from hermes_cli.fallback_config import get_fallback_chain
    complete = True
    for raw in (source.get('fallback_providers'), source.get('fallback_model')):
        candidates = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        complete = complete and (raw is None or isinstance(raw, (dict, list))) and all(
            isinstance(e, dict) and e.get('provider') and e.get('model') for e in candidates)
    providers = [_route(entry, str(entry['model']).strip())[0] for entry in get_fallback_chain(source)]
    complete = complete and all(row['credential_kind'] != 'unknown' for row in providers)
    legacy = source.get('fallback_model')
    return {'providers': providers, 'legacy_fallback_model': legacy.get('model') if isinstance(legacy, dict) else None,
            'chain_complete': complete}


def _checkpoint_required(cfg):
    section = cfg.get('compression')
    if section is None:
        return False
    if not isinstance(section, dict):
        return None
    value = section.get('checkpoint_required')
    if value is None:
        return False
    return value if isinstance(value, bool) else None


def _observers():
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    rows = [{'name': p.manifest.name, 'events': sorted(p.hooks_registered)}
            for p in manager._plugins.values() if p.enabled and not p.error and p.hooks_registered]
    return rows, manager._discovered


def _runtime_policy(cfg, source):
    """The projection. ``source`` is the route's config (the frozen snapshot for a config-only session,
    else the live profile config); hooks, webhooks, observers and compression are the live registries."""
    from agent.outbound_webhooks import iter_configured_targets
    from agent.shell_hooks import consented_hook_state
    model = source.get('model')
    route = model if isinstance(model, dict) else {}
    name = model if isinstance(model, str) else route.get('default') or route.get('model')
    identity, base = _route(route, str(name or '').strip())
    primary = {**identity, 'endpoint_host': urlsplit(base).hostname or '' if base else ''}
    fallback = _fallback(source)
    events, hooks_complete = consented_hook_state(cfg)
    targets = iter_configured_targets(cfg)
    observers, observers_complete = _observers()
    checkpoint = _checkpoint_required(cfg)
    sections = {
        'primary': primary,
        'fallback': fallback,
        'hooks_active': events,
        'outbound_webhooks': {'count': len(targets), 'events': sorted({e for t in targets for e in t.events}),
                              'hosts': sorted({urlsplit(t.url).hostname or '' for t in targets} - {''})},
        'observer_plugins': observers,
        'compression': {'checkpoint_required': checkpoint},
    }
    primary_known = (all(primary[k] for k in ('provider', 'model', 'endpoint_host'))
                     and primary['credential_kind'] != 'unknown')
    complete = (primary_known and fallback['chain_complete'] and hooks_complete and observers_complete
                and checkpoint is not None)
    digest = hashlib.sha256(json.dumps(sections, sort_keys=True).encode()).hexdigest()[:16]
    return {**sections, 'revision': digest, 'complete': complete}


async def runtime_policy(connection, ref, params):
    """Main-only (the native owner connection), read-only projection of the effective runtime route:
    what a managed client checks before its first prompt. Non-secret by construction."""
    if not connection.native_owner:
        raise RuntimeStoreError('permission_denied')
    policy = _authorize(connection, ref, params)
    home = Path(connection.authority.profile_id)

    def read():
        from gateway.run import _profile_runtime_scope
        from hermes_cli.config import load_config
        with _profile_runtime_scope(home):
            cfg = load_config()
            source = policy.config(connection.authority, secrets=False) if (
                policy is not None and policy.ignore_user_config) else cfg
            return {'runtime_policy': _runtime_policy(cfg, source)}
    return await asyncio.to_thread(read)


async def model_options(connection, ref, params):
    flags = {'explicit_only', 'include_unconfigured', 'refresh'}
    if (set(params) - flags - {'profile', 'session_id'}
            or any(type(params[k]) is not bool for k in flags & params.keys())):
        raise RuntimeStoreError('invalid_params')
    policy = _authorize(connection, ref, params)
    agent = connection.authority.agent(ref) if ref.session_id else None
    provider = getattr(agent, 'provider', None) or (policy.provider if policy else None)
    model = getattr(agent, 'model', None) or (policy.model if policy else None)
    base_url = getattr(agent, 'base_url', None) or (policy.base_url if policy else None)

    def discover():
        from gateway.run import _profile_runtime_scope
        from hermes_cli.inventory import load_picker_context, build_model_options_payload
        with _profile_runtime_scope(Path(connection.authority.profile_id)):
            ctx = load_picker_context()
            selected = provider
            if selected == 'custom':
                from hermes_cli.runtime_provider import canonical_custom_identity
                selected = canonical_custom_identity(base_url=base_url, config_provider=ctx.current_provider, model=model,
                                                     requested=getattr(agent, 'requested_provider', None)) or selected
            return build_model_options_payload(ctx.with_overrides(current_provider=selected,
                current_model=model, current_base_url=base_url), **{k: params[k] for k in flags & params.keys()})
    return await asyncio.to_thread(discover)
