"""A deleted recorded session cwd must not poison later local commands (exit 126)."""

from tools import terminal_tool


def test_deleted_recorded_cwd_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    gone = tmp_path / "gone"
    gone.mkdir()
    terminal_tool.record_session_cwd("stale-key", str(gone))
    gone.rmdir()

    cwd = terminal_tool._resolve_command_cwd(
        workdir=None,
        default_cwd="/default",
        session_key="stale-key",
        env_type="local",
    )

    assert cwd == "/default"
