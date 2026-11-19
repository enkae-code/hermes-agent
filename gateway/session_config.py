"""Owner-bound native settings and model inventory, never legacy execution."""
import asyncio
from collections import Counter
from functools import partial
import hashlib
import json
import logging
import os
import re
import threading
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
        return await runtime_policy(connection, ref, params, policy)
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
# Public identifiers only. Config values can carry expanded secrets, so a value is emitted only when it is a
# short token of this grammar; containers, ``${VAR}`` refs and free text are never stringified into a field.
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}')
_HOST_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
_HOST = re.compile(rf'{_HOST_LABEL}(?:\.{_HOST_LABEL})*')


def _ident(value):
    """``value`` as a public identifier, else None."""
    text = value.strip() if isinstance(value, str) else ''
    return text if _IDENT.fullmatch(text) else None


def _host(url):
    """The hostname of ``url`` alone: userinfo, port, path and query never reach the result."""
    try:
        host = urlsplit(url).hostname if isinstance(url, str) and url else None
    except ValueError:
        host = None
    return host if host and _HOST.fullmatch(host) else ''


def _literal_key(value):
    """A key the route names itself. A ``${VAR}`` reference, an empty value or a non-string is not a key."""
    return isinstance(value, str) and bool(value.strip()) and '$' not in value


def _route(entry, model, key_present=False, env=frozenset(), named=False):
    """``(identity, base_url)`` for one route. An api_key provider is ``api_key`` only when the route holds a
    key: a literal one in its config, ``key_present`` (the session's secret map or a bound launch key, by
    presence only), or a declared key var non-empty in the profile ``.env`` (``env``, presence only). An
    unresolved key reads ``unknown``. A provider outside the catalog, or any route through a ``providers:``
    entry (``named``), is not catalog-derived: kind and host are unknown. An env endpoint override or an
    unresolved ``${VAR}`` makes the host unknown. Nothing from the key, the URL path, query or headers is returned."""
    from hermes_cli.providers import get_provider
    provider = _ident(entry.get('provider'))
    pdef = get_provider(provider, allow_network=False) if provider and not named else None
    kind = _CREDENTIAL_KINDS.get(pdef.auth_type) if pdef else None
    if kind == 'api_key' and not (key_present or _literal_key(entry.get('api_key'))
                                  or any(var in env for var in pdef.api_key_env_vars)):
        kind = None
    base = entry.get('base_url') or (pdef.base_url if pdef else '')
    if pdef and pdef.base_url_env_var and (pdef.base_url_env_var in env
                                           or os.environ.get(pdef.base_url_env_var, '').strip()):
        base = ''
    if named or not isinstance(base, str) or '$' in base:
        base = ''
    identity = {'provider': provider if pdef else None, 'model': _ident(model), 'credential_kind': kind or 'unknown'}
    return identity, base


def _primary(source, *, present=False, env=frozenset(), named=False):
    model = source.get('model')
    route = model if isinstance(model, dict) else {}
    name = model if isinstance(model, str) else route.get('default') or route.get('model')
    identity, base = _route(route, name, present, env, named)
    return {**identity, 'endpoint_host': _host(base)}


def _named(*docs):
    """True when a config names a custom provider execution could select (``providers:`` / ``custom_providers:``)."""
    return any(bool(doc.get(key)) for doc in docs for key in ('providers', 'custom_providers'))


def _fallback(source, env=frozenset(), named=False):
    from hermes_cli.fallback_config import get_fallback_chain
    complete = True
    raws = (source.get('fallback_providers'), source.get('fallback_model'))
    for raw in raws:
        candidates = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        complete = complete and (raw is None or isinstance(raw, (dict, list))) and all(
            isinstance(e, dict) and e.get('provider') and e.get('model') for e in candidates)
    # An unresolved ``${VAR}`` in any entry means the chain is not what execution will resolve.
    complete = complete and not any('${' in json.dumps(raw, default=str) for raw in raws)
    providers = [_route(entry, entry.get('model'), env=env, named=named)[0] for entry in get_fallback_chain(source)]
    complete = complete and all(
        row['credential_kind'] != 'unknown' and row['provider'] and row['model'] for row in providers)
    legacy_raw = source.get('fallback_model')
    legacy = _ident(legacy_raw.get('model')) if isinstance(legacy_raw, dict) else None
    complete = complete and (legacy_raw is None or legacy is not None)
    return {'providers': providers, 'legacy_fallback_model': legacy, 'chain_complete': complete}


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


def _observers(home_doc):
    """``(rows, complete)``: the lifecycle observers that run, from the live dispatch registry. Never discovers,
    waits or calls ``enabled()`` (which can tear a runtime down). A callback the registry holds that no plugin
    row, shell hook or outbound webhook accounts for (a memory provider's fallback hook) is incomplete."""
    from agent.safe_worker_policy import safe_worker_enabled
    if safe_worker_enabled():
        return [], True
    from agent.outbound_webhooks import registered_targets
    from agent.shell_hooks import registered_event_counts
    from hermes_cli.observability.relay_shared_metrics import HANDLED_HOOKS
    from hermes_cli.plugins import _loaded_registry_nowait

    def dispatch(manager):
        rows = [{'name': p.manifest.name, 'events': sorted(p.hooks_registered)}
                for p in manager._plugins.values() if p.enabled and not p.error and p.hooks_registered]
        # ponytail: one attributed callback per plugin row per event; a plugin registering twice reads incomplete.
        attributed = Counter(event for row in rows for event in row['events'])
        attributed.update(registered_event_counts())
        attributed.update(event for event, _ in registered_targets())
        dispatched = Counter({event: len(callbacks) for event, callbacks in manager._hooks.items() if callbacks})
        return rows, dispatched == attributed

    result = _loaded_registry_nowait(dispatch)
    if result is False:
        return [], False
    rows, accounted = result
    telemetry = home_doc.get('telemetry')
    shared = telemetry.get('shared_metrics') if isinstance(telemetry, dict) else None
    if isinstance(shared, dict) and shared.get('enabled') is True:
        rows.append({'name': 'shared_metrics', 'events': sorted(HANDLED_HOOKS)})
    return rows, accounted and all(_ident(row['name']) == row['name'] for row in rows)


def _revision(sections, complete):
    """Digest of every projected field and ``complete``; set-like observer rows in canonical order."""
    rows = sorted(sections['observer_plugins'], key=lambda row: json.dumps(row, sort_keys=True))
    payload = {**sections, 'observer_plugins': rows, 'complete': complete}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class _QuietOwnThread(logging.Filter):
    """Drops the records one thread logs. A read validates config that logs its raw values, and only that read is silenced."""

    def __init__(self):
        super().__init__()
        self.ident = threading.get_ident()

    def filter(self, record):
        return threading.get_ident() != self.ident


def _quiet(logger_name, fn, *args):
    """``fn(*args)`` with the calling thread's records from ``logger_name`` dropped (no config value is logged)."""
    logger = logging.getLogger(logger_name)
    quiet = _QuietOwnThread()
    logger.addFilter(quiet)
    try:
        return fn(*args)
    finally:
        logger.removeFilter(quiet)


def _registries(home_doc, source):
    """One sample of every live registry the projection reports: shell hooks (consent, parse), wired outbound
    webhooks and observers. Called twice per read; a difference between the samples is an unstable read."""
    from agent.outbound_webhooks import registered_targets
    from agent.shell_hooks import consented_hook_state
    events, hooks_complete = _quiet('agent.shell_hooks', consented_hook_state, source)
    observers, observers_complete = _observers(home_doc)
    return events, hooks_complete, registered_targets(), observers, observers_complete


def _chain_view(cfg, env, named):
    """What execution reads from a config for the fallback chain and checkpoint: the agreement key."""
    fallback = _fallback(cfg, env, named)
    return fallback['providers'], fallback['legacy_fallback_model'], fallback['chain_complete'], _checkpoint_required(cfg)


def _resident_fallback_active(agent):
    """True when a resident agent for this session is running a fallback route (its primary is not in use)."""
    return getattr(agent, '_fallback_activated', False) is True


def _retained_disagrees(home, doc, env, named):
    """True when the config this process last loaded for the home holds a different chain or checkpoint than the
    file now: a retained last-known-good an execution could still read. Read from the loader's cache, never mutated."""
    from hermes_cli.config import _LAST_EXPANDED_CONFIG_BY_PATH
    retained = _LAST_EXPANDED_CONFIG_BY_PATH.get(str(home / 'config.yaml'))
    return retained is not None and _chain_view(retained, env, named) != _chain_view(doc, env, named)


def _runtime_policy(home_doc, source, *, chain, chain_known, checkpoint, secrets, bound, session, managed, known,
                    followed=False, live=None, env=frozenset(), named=False):
    """The projection of one route. ``source`` is the route the session pins (or the home's config, sessionless);
    ``live`` is the profile's current config, which every execution path may also read: a disagreement between
    the two (primary, chain, checkpoint) is incomplete, and the live primary is reported. ``chain`` and
    ``checkpoint`` are the sources execution reads them from. ``secrets``: the config paths whose values the
    session holds out of its config; ``bound``: a launch key is bound. Hooks, webhooks and observers come from
    the live registries, sampled twice: a difference between samples is incomplete. The YAML is never re-parsed."""
    present = ('model', 'api_key') in secrets or bound
    primary = _primary(source, present=present, env=env, named=named)
    agreed = True
    if live is not None:
        live_primary = _primary(live, env=env, named=named)
        agreed = (primary == live_primary and _chain_view(source, env, named) == _chain_view(live, env, named)
                  and _checkpoint_required(source) == _checkpoint_required(live))
        primary = live_primary
    fallback = _fallback(chain, env, named)
    fallback['chain_complete'] = fallback['chain_complete'] and chain_known
    first = _registries(home_doc, source)
    stable = first == _registries(home_doc, source)
    events, hooks_complete, wired, observers, observers_complete = first
    raw_hooks = source.get('hooks')
    raw_outbound = raw_hooks.get('outbound') if isinstance(raw_hooks, dict) else None
    hosts = {_host(url) for _, url in wired}
    # ponytail: structural entry check, not the validating parser (which resolves secret_env); a wired target is in the registry.
    webhooks_complete = '' not in hosts and (raw_outbound is None or (isinstance(raw_outbound, list) and all(
        isinstance(entry, dict) and isinstance(entry.get('url'), str) and _host(entry['url']) for entry in raw_outbound)))
    sections = {
        'primary': primary,
        'fallback': fallback,
        'hooks_active': events,
        'outbound_webhooks': {'count': len({url for _, url in wired}),
                              'events': sorted({event for event, _ in wired}),
                              'hosts': sorted(hosts - {''})},
        'observer_plugins': observers,
        'compression': {'checkpoint_required': checkpoint},
    }
    primary_known = (all(primary[k] for k in ('provider', 'model', 'endpoint_host'))
                     and primary['credential_kind'] != 'unknown')
    complete = (session and not managed and not followed and known and agreed and stable and primary_known
                and fallback['chain_complete'] and hooks_complete and webhooks_complete and observers_complete
                and checkpoint is not None)
    return {**sections, 'revision': _revision(sections, complete), 'complete': complete}


def _env_names(path):
    """Names the profile's ``.env`` assigns a non-empty value, presence only: no value leaves this read, and no
    ``os.environ`` change. None when the file cannot be read (incomplete, fail closed)."""
    if not path.exists():
        return frozenset()
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    names = set()
    for line in lines:
        line = line.strip()
        if line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.removeprefix('export ').partition('=')
        if value.strip().strip('\'"'):
            names.add(key.strip())
    return frozenset(names)


def _home_state(home):
    """``(doc, readable)``: the profile's own config.yaml, read with no write, hydration or env expansion. A
    remote config backend is not fetched here: its state is not published locally, so it is unreadable."""
    from hermes_cli.config_backend import get_config_backend, read_config_doc_readonly
    if not get_config_backend().honors_managed_config():
        return {}, False
    try:
        doc = read_config_doc_readonly(home / 'config.yaml')
    except Exception:  # health: allow BLE001 -- refusal gate: any read failure reports unreadable (fail closed)
        return {}, False
    if doc is None:
        return {}, True
    return (doc, True) if isinstance(doc, dict) else ({}, False)


def _managed_overlay_active():
    from hermes_cli.managed_scope import load_managed_config
    try:
        return bool(load_managed_config(strict=True))
    except Exception:  # health: allow BLE001 -- refusal gate: an unreadable overlay counts as active (fail closed)
        return True


def _managed_worker(policy):
    """True when the session executes in a managed worker, or whether it does cannot be told without starting one."""
    from gateway import session_managed_worker
    try:
        return session_managed_worker.policy_if_managed(policy) is not None
    except RuntimeStoreError:
        return True


def _secret_paths(authority, policy):
    """The config paths whose secret values the session holds out of its config. Presence only: no value is read."""
    from gateway.session_policy_credentials import PREFIX
    ref = policy.config_secret_ref
    if ref is None:
        return set()
    if ref.startswith(PREFIX):
        return {tuple(path) for path, _ in json.loads(ref[len(PREFIX):])['entries']}
    return set(getattr(authority, '_local_config_secrets', {}).get(ref) or ())


async def runtime_policy(connection, ref, params, policy):
    """Native-owner connection only, read-only projection of the effective runtime route: what a managed client
    checks before its first prompt. Non-secret by construction. Every section reads the frozen session snapshot
    AND the owning profile (what execution may read); a section is complete only where they agree. A sessionless
    read is informational and never complete."""
    if not connection.native_owner:
        raise RuntimeStoreError('permission_denied')
    home = Path(connection.authority.profile_id)
    agent = connection.authority.agent(ref) if policy is not None else None

    def read():
        from gateway.session_policy import present_sections
        doc, home_ok = _home_state(home)
        env = _env_names(home / '.env')
        home_ok = home_ok and env is not None
        env = env or frozenset()
        live_ok = home_ok and not _managed_overlay_active()
        if policy is None:
            named = _named(doc)
            known = live_ok and not named and not _retained_disagrees(home, doc, env, named)
            return {'runtime_policy': _runtime_policy(
                doc, doc, chain=doc, chain_known=known, checkpoint=_checkpoint_required(doc) if live_ok else None,
                secrets=set(), bound=False, session=False, managed=False, known=home_ok, env=env, named=named)}
        frozen = present_sections(json.loads(policy.config_json))
        named = _named(frozen, doc)
        resident_ok = not _resident_fallback_active(agent)
        if policy.safe_mode:
            chain, chain_known = {}, resident_ok and not named
        elif policy.ignore_user_config:
            chain, chain_known = frozen, resident_ok and not named
        else:
            chain = doc
            chain_known = (live_ok and resident_ok and not named
                           and not _retained_disagrees(home, doc, env, named))
        checkpoint_known = policy.ignore_user_config or live_ok
        checkpoint_source = frozen if policy.ignore_user_config else doc
        return {'runtime_policy': _runtime_policy(
            doc, frozen, chain=chain, chain_known=chain_known,
            checkpoint=_checkpoint_required(checkpoint_source) if checkpoint_known else None,
            secrets=_secret_paths(connection.authority, policy), bound=policy.credential_ref is not None,
            session=True, managed=_managed_worker(policy), known=home_ok, followed=bool(policy.followed_sections),
            live=None if policy.safe_mode else doc, env=env, named=named)}
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
