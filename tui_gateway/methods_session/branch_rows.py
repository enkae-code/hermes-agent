"""Branch child-row persistence (``methods_session`` split).

Moved verbatim from ``tui_gateway/methods_session.py`` (file-line ratchet): the body closes over
server.py globals through ``method_ctx.bind_module`` exactly as before.
"""

from ..method_ctx import bind_module


def _persist_branch(db, new_key: str, parent_key: str, title: str, history: list, *, source, cwd, profile_name,
                    model: str, copy_fields=(), compensate: bool = False, title_source: str = "user",
                    user_id: str | None = None) -> None:
    """Branch child row + parent transcript (bounded-chunk transactions) + title. ``_branched_from`` keeps the
    row visible in list_sessions_rich() (the live parent never matches the legacy end_reason='branched'
    heuristic); NULL ``profile_name`` rows drop out of profile-keyed sidebar matching / deep links. ``compensate``
    deletes a committed row whose transcript/title failed (a durable-but-empty row would defeat the INSERT OR
    IGNORE first-prompt seed) — except on disk-full, where the delete cannot land. ``user_id`` is the creating
    login: the child is a Desktop session too, and the row only records identity at insert."""
    from agent.message_metadata import message_identity
    from agent.transcript_repair import sync_flushed_message_markers

    # The child sends the parent's exact system prompt: a row without one makes the branch's first
    # turn rebuild (re-probing the workspace) and forfeits the warm cache the copied transcript buys.
    parent_prompt = None
    try:
        parent_prompt = (db.get_session(parent_key) or {}).get("system_prompt")
    except Exception:
        logger.debug("branch: parent system prompt read failed for %s", parent_key, exc_info=True)
    db.create_session(new_key, source=source, model=model, model_config={"_branched_from": parent_key},
                      parent_session_id=parent_key, cwd=cwd, profile_name=profile_name, user_id=user_id,
                      system_prompt=parent_prompt or None)
    try:
        # Compensation guard (#93959 review): if the transcript copy or title write fails AFTER the row
        # committed, the durable-but-empty row would defeat the lazy first-prompt fallback
        # (_ensure_session_db_row is INSERT OR IGNORE — the row exists, so the seed never lands and the
        # renderer fail-latches on a "transcript-less" session again). Roll back just this child so the seed
        # path can retry cleanly on first submit.
        # Copy the whole parent history in bounded-chunk transactions — a branch seed can be hundreds of
        # rows, and per-row transactions were the write-amplification pattern removed in #23254.
        rows = [{"role": msg.get("role", "user"), "content": msg.get("content"),
                 **{field: msg.get(field) for field in copy_fields}, **message_identity(msg)} for msg in history]
        db.append_messages_batch(new_key, rows, chunk_rows=500)
        if title_source == "user":
            db.set_session_title(new_key, title)
        else:
            db.set_auto_title(new_key, title, source=title_source)
        sync_flushed_message_markers(history, rows)
    except Exception as exc:
        from hermes_state_errors import is_disk_full_error
        if compensate and not is_disk_full_error(exc):
            try:
                db.discard_unadmitted_session(new_key)
            except Exception:
                logger.debug("branch seed compensation delete failed for %s", new_key, exc_info=True)
        raise


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals)."""
    bind_module(globals(), server, skip=("_",))
