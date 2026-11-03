"""A content-search pattern that starts with '-' is a pattern, not an option.

rg and grep read ``-dash-pattern`` as flags unless the pattern follows ``--``.
"""

import shutil

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

_METHODS = ["_search_with_grep"]
if shutil.which("rg"):
    _METHODS.append("_search_with_rg")


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("pattern", ["-dash-pattern", "--foo"])
def test_dash_leading_pattern_finds_line(method, pattern, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text(f"before\nlead {pattern} tail\nafter\n")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))

    res = getattr(ops, method)(pattern, str(tmp_path), None, 50, 0, "content", 0)

    assert res.error is None
    assert [m.line_number for m in res.matches] == [2]


@pytest.mark.skipif(not shutil.which("rg"), reason="zero-match probe runs only through rg")
def test_dash_pattern_zero_match_probe_has_no_error(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing here\n")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))

    res = ops.search("-dash-pattern", str(tmp_path))

    assert res.error is None
    assert res.matches == []
