"""Owner-bound native settings and model inventory, never legacy execution."""
import asyncio
from collections import Counter
from functools import partial
import hashlib
import json
import logging
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


def _primary(source, locked_policy):
    """The primary route a client is shown. A locked session reports its frozen route (read from the policy alone);
    anything else is an unverified claim of the config, never a credential kind."""
    if locked_policy is not None:
        from gateway.session_route_lock import preview
        provider, model, base_url = preview(locked_policy)
        return {'provider': _ident(provider), 'model': _ident(model), 'credential_kind': 'api_key',
                'endpoint_host': _host(base_url)}
    model = source.get('model')
    route = model if isinstance(model, dict) else {}
    name = model if isinstance(model, str) else route.get('default') or route.get('model')
    return {'provider': _ident(route.get('provider')), 'model': _ident(name), 'credential_kind': 'unknown',
            'endpoint_host': ''}


def _named(*docs):
    """True when a config names a route the certified shape cannot describe: a custom provider (``providers:`` /
    ``custom_providers:``) or an external secret source (``secrets:``), which is never run here."""
    return any(bool(doc.get(key)) for doc in docs for key in ('providers', 'custom_providers', 'secrets'))


def _fallback(source):
    """The chain as execution reads it. S7: any entry or legacy ``fallback_model`` leaves the chain incomplete."""
    from hermes_cli.fallback_config import get_fallback_chain
    entries = get_fallback_chain(source)
    providers = [_entry_identity(entry) for entry in entries]
    legacy_raw = source.get('fallback_model')
    legacy = _ident(legacy_raw.get('model')) if isinstance(legacy_raw, dict) else None
    complete = not entries and not any(source.get(key) for key in ('fallback_providers', 'fallback_model'))
    return {'providers': providers, 'legacy_fallback_model': legacy, 'chain_complete': complete}


def _entry_identity(entry):
    return {'provider': _ident(entry.get('provider')), 'model': _ident(entry.get('model'))}


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
    valid = all(_ident(row['name']) == row['name'] and all(_ident(event) == event for event in row['events'])
                for row in rows)
    labelled = [{'name': _label(row['name']), 'events': [_label(event) for event in row['events']]} for row in rows]
    return labelled, accounted and valid


def _label(value):
    """A public identifier as-is; a free-text plugin name or event never reaches the projection."""
    return value if isinstance(value, str) and _ident(value) == value else '<invalid>'


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


def _hook_identity():
    """Each event's live callbacks by object identity, so a replaced hook is a different callback under the same name.
    The samples hold their callbacks, so an identity is never reused between samples. Read-only."""
    from hermes_cli.plugins import _loaded_registry_nowait
    return _loaded_registry_nowait(lambda manager: {event: tuple(cbs) for event, cbs in manager._hooks.items() if cbs})


def _same_callbacks(first, second):
    """True when two identity samples hold the same callbacks by object identity. Never ``==``: a plugin callable's
    ``__eq__`` can run, and an unavailable sample (``False``) is not agreement."""
    if not isinstance(first, dict) or not isinstance(second, dict) or first.keys() != second.keys():
        return False
    return all(len(first[event]) == len(second[event]) and all(a is b for a, b in zip(first[event], second[event]))
               for event in first)


def _registries(home_doc, source):
    """One sample of every live registry the projection reports: shell hooks (consent, parse), wired outbound
    webhooks, observers and the callback identities. Called twice per read; a difference between the samples is an
    unstable read."""
    from agent.outbound_webhooks import registered_targets
    from agent.shell_hooks import consented_hook_state
    events, hooks_complete = _quiet('agent.shell_hooks', consented_hook_state, source)
    observers, observers_complete = _observers(home_doc)
    return events, hooks_complete, registered_targets(), observers, observers_complete, _hook_identity()


def _chain_key(entries):
    """Ordered route identities of a fallback chain: provider, model, endpoint, api_mode, key presence and whether each
    entry declares a credential (presence only, never its value). Compared in memory before anything is returned, so an
    endpoint-only or credential-declaration-only change is a different route."""
    return [json.dumps([_entry_identity(entry), entry.get('base_url'),
                        entry.get('api_mode'), [bool(entry.get(key)) for key in ('api_key', 'key_env', 'api_key_env')]],
                       sort_keys=True, default=str) for entry in entries]


def _chain_view(cfg):
    """What execution reads from a config for the fallback chain and checkpoint: the agreement key."""
    from hermes_cli.fallback_config import get_fallback_chain
    fallback = _fallback(cfg)
    return (_chain_key(get_fallback_chain(cfg)), fallback['legacy_fallback_model'],
            fallback['chain_complete'], _checkpoint_required(cfg))


def _runner_chains_agree(runner, home, entries):
    """True when the runner's retained fallback state for this home (``_fallback_model_by_home``, keyed as the loader in
    ``gateway/run_config_loaders.py`` keys it) and its ``_fallback_model`` default both match the live chain. Read-only,
    compared in memory. An unreachable runner is not agreement."""
    from hermes_constants import hermes_home_key
    by_home = getattr(runner, '_fallback_model_by_home', None) or {}
    if not isinstance(by_home, dict) or not hasattr(runner, '_fallback_model'):
        return False
    live = _chain_key(entries)
    # A home the loader never refreshed has no retained chain of its own; the runner's default still must agree.
    retained = [by_home[hermes_home_key(home)]] if hermes_home_key(home) in by_home else []
    return all(_chain_key(chain or []) == live for chain in [*retained, runner._fallback_model])


def _resident_fallback_active(agent):
    """True when a resident agent for this session is running a fallback route (its primary is not in use)."""
    return getattr(agent, '_fallback_activated', False) is True


def _retained_disagrees(home, doc):
    """True when the config this process last loaded for the home holds a different chain or checkpoint than the
    file now: a retained last-known-good an execution could still read. Read from the loader's cache, never mutated."""
    from hermes_cli.config import _LAST_EXPANDED_CONFIG_BY_PATH
    retained = _LAST_EXPANDED_CONFIG_BY_PATH.get(str(home / 'config.yaml'))
    return retained is not None and _chain_view(retained) != _chain_view(doc)


def _runtime_policy(home_doc, source, *, chain, chain_known, checkpoint, locked, session, managed, known,
                    followed=False, live=None):
    """The projection of one route. ``source`` is the route the session pins (or the home's config, sessionless);
    ``live`` is the profile's current config, which every execution path may also read: a disagreement between
    the two (chain, checkpoint) is incomplete. ``locked``: the session's policy when it holds a route lock, whose
    frozen route is the only primary reported complete. Hooks, webhooks and observers come from the live
    registries, sampled twice: a difference between samples is incomplete. The YAML is never re-parsed."""
    agreed = live is None or (_chain_view(source) == _chain_view(live)
                              and _checkpoint_required(source) == _checkpoint_required(live))
    shown = live if live is not None else source  # an unlocked session reports the route the profile now holds
    fallback = _fallback(chain)
    fallback['chain_complete'] = fallback['chain_complete'] and chain_known
    first = _registries(home_doc, source)
    second = _registries(home_doc, source)
    stable = first[:-1] == second[:-1] and _same_callbacks(first[-1], second[-1])
    events, hooks_complete, wired, observers, observers_complete, _ = first
    raw_hooks = source.get('hooks')
    raw_outbound = raw_hooks.get('outbound') if isinstance(raw_hooks, dict) else None
    hosts = {_host(url) for _, url in wired}
    # ponytail: structural entry check, not the validating parser (which resolves secret_env); a wired target is in the registry.
    webhooks_complete = '' not in hosts and (raw_outbound is None or (isinstance(raw_outbound, list) and all(
        isinstance(entry, dict) and isinstance(entry.get('url'), str) and _host(entry['url']) for entry in raw_outbound)))
    sections = {
        'primary': _primary(shown, locked),
        'fallback': fallback,
        'hooks_active': events,
        'outbound_webhooks': {'count': len({url for _, url in wired}),
                              'events': sorted({event for event, _ in wired}),
                              'hosts': sorted(hosts - {''})},
        'observer_plugins': observers,
        'compression': {'checkpoint_required': checkpoint},
    }
    complete = (locked is not None and session and not managed and not followed and known and agreed and stable
                and fallback['chain_complete'] and hooks_complete and webhooks_complete and observers_complete
                and checkpoint is not None)
    return {**sections, 'revision': _revision(sections, complete), 'complete': complete}


def _file_backend():
    """True when the config backend is the local file backend. Decided from the backend's type alone, before any
    scope or read, so a remote backend's config is never read or prepared for a terminal policy."""
    from hermes_cli.config_backend import FileBackend, get_config_backend
    try:
        return isinstance(get_config_backend(), FileBackend)
    except (Exception, SystemExit):  # health: allow BLE001 -- an unselectable backend is not the local file (fail closed)
        return False


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


async def runtime_policy(connection, ref, params, policy):
    """Native-owner connection only, read-only projection of the effective runtime route: what a managed client
    checks before its first prompt. Non-secret by construction. Every section reads the frozen session snapshot
    AND the owning profile (what execution may read); a section is complete only where they agree. A sessionless
    read is informational and never complete."""
    if not connection.native_owner:
        raise RuntimeStoreError('permission_denied')
    return await projected_runtime_policy(connection, ref, policy)


async def projected_runtime_policy(connection, ref, policy):
    """The projection itself, for the getter (after its native-owner gate) and the ``prompt.submit`` revision fence."""
    home = Path(connection.authority.profile_id)
    agent = connection.authority.agent(ref) if policy is not None else None
    runner = getattr(connection.authority, 'runner', None)

    def read():
        from gateway import session_route_lock
        from gateway.run import _profile_runtime_scope
        from gateway.session_policy import present_sections
        from hermes_cli.fallback_config import get_fallback_chain
        if not _file_backend():  # decided before any scope: a remote backend is never read or prepared
            return {'runtime_policy': _runtime_policy(
                {}, {}, chain={}, chain_known=False, checkpoint=None, locked=None,
                session=policy is not None, managed=False, known=False)}
        # Unhydrated: a getter never runs a configured secret source; it reads only the published snapshot.
        with _profile_runtime_scope(home, hydrate_secrets=False):
            doc, home_ok = _home_state(home)
            live_ok = home_ok and not _managed_overlay_active()
            if policy is None:
                named = _named(doc)
                retained_ok = _runner_chains_agree(runner, home, get_fallback_chain(doc))
                known = live_ok and not named and retained_ok and not _retained_disagrees(home, doc)
                return {'runtime_policy': _runtime_policy(
                    doc, doc, chain=doc, chain_known=known, checkpoint=_checkpoint_required(doc) if live_ok else None,
                    locked=None, session=False, managed=False, known=home_ok)}
            frozen = present_sections(json.loads(policy.config_json))
            named = _named(frozen, doc)
            resident_ok = not _resident_fallback_active(agent)
            retained_ok = resident_ok and _runner_chains_agree(runner, home, get_fallback_chain(doc))
            if policy.safe_mode:
                chain, chain_known = {}, retained_ok and not named
            elif policy.ignore_user_config:
                chain, chain_known = frozen, retained_ok and not named
            else:
                chain = doc
                chain_known = (live_ok and retained_ok and not named
                               and not _retained_disagrees(home, doc))
            checkpoint_known = policy.ignore_user_config or live_ok
            checkpoint_source = frozen if policy.ignore_user_config else doc
            return {'runtime_policy': _runtime_policy(
                doc, frozen, chain=chain, chain_known=chain_known,
                checkpoint=_checkpoint_required(checkpoint_source) if checkpoint_known else None,
                locked=policy if session_route_lock.is_locked(policy) else None,
                session=True, managed=_managed_worker(policy), known=home_ok, followed=bool(policy.followed_sections),
                live=None if policy.safe_mode else doc)}
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
