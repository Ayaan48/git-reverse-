"""Code views for the Problems panel: excerpts, GitHub-style diffs, and
whether each problem was really resolved."""

from healing_agent.code_views import (
    build_code_views,
    build_file_diff,
    capture_excerpts,
    problem_status,
)
from healing_agent.models import Problem, ProblemKind, Severity
from test_pipeline_integration import _run


def _problem(file, line, code="E711", severity=Severity.MEDIUM):
    return Problem(file=file, line=line, column=1, kind=ProblemKind.LINT,
                   severity=severity, code=code, message=f"{code} here", detector="ruff")


def _rows(diff):
    return [(r["type"], r.get("old"), r.get("new"), r["text"])
            for hunk in diff["hunks"] for r in hunk["rows"]]


# -------------------------------------------------------------------- diffs --


def test_a_changed_line_is_a_removal_and_an_addition_with_context():
    diff = build_file_diff("a.py", "a\nb\nc\n", "a\nB\nc\n")
    assert _rows(diff) == [
        ("context", 1, 1, "a"),
        ("del", 2, None, "b"),
        ("add", None, 2, "B"),
        ("context", 3, 3, "c"),
    ]
    assert (diff["additions"], diff["deletions"]) == (1, 1)
    hunk = diff["hunks"][0]
    assert (hunk["old_start"], hunk["old_count"], hunk["new_start"], hunk["new_count"]) == (1, 3, 1, 3)


def test_whitespace_only_changes_are_shown():
    diff = build_file_diff("a.py", "def f():\n\treturn 1\n", "def f():\n    return 1\n")
    assert ("del", 2, None, "\treturn 1") in _rows(diff)
    assert ("add", None, 2, "    return 1") in _rows(diff)


def test_adding_a_missing_final_newline_is_visible():
    diff = build_file_diff("a.py", "x\ny", "x\ny\n")
    rows = [r for hunk in diff["hunks"] for r in hunk["rows"]]
    kinds = [r["type"] for r in rows]
    assert kinds == ["context", "del", "note", "add"]
    assert rows[2]["text"] == "No newline at end of file"


def test_only_nearby_lines_are_kept_as_context():
    old = "".join(f"line {n}\n" for n in range(1, 41))
    new = old.replace("line 20\n", "LINE 20\n")
    diff = build_file_diff("a.py", old, new)
    olds = [r["old"] for r in diff["hunks"][0]["rows"] if r["old"]]
    assert min(olds) == 17 and max(olds) == 23, "3 lines of context each side"


def test_long_diffs_are_truncated_and_say_so():
    old = "".join(f"a{n}\n" for n in range(300))
    new = "".join(f"b{n}\n" for n in range(300))
    diff = build_file_diff("big.py", old, new, max_rows=50)
    assert sum(len(h["rows"]) for h in diff["hunks"]) == 50
    assert diff["truncated"] is True
    assert diff["additions"] == 300 and diff["deletions"] == 300, "totals stay exact"


# ----------------------------------------------------------------- excerpts --


def test_excerpts_merge_nearby_problems_and_clamp_to_the_file(tmp_path):
    (tmp_path / "a.py").write_text("".join(f"l{n}\n" for n in range(1, 21)))
    excerpts = capture_excerpts(tmp_path, [_problem("a.py", 5), _problem("a.py", 7),
                                           _problem("a.py", 19), _problem("a.py", 99)])
    ranges = excerpts[0]["ranges"]
    assert [(r["start"], len(r["lines"])) for r in ranges] == [(3, 7), (17, 4)]
    assert ranges[0]["lines"][0] == "l3"


def test_excerpts_put_the_worst_file_first(tmp_path):
    (tmp_path / "minor.py").write_text("x = 1\n")
    (tmp_path / "broken.py").write_text("x = (\n")
    excerpts = capture_excerpts(tmp_path, [
        _problem("minor.py", 1, "W291", Severity.LOW),
        _problem("broken.py", 1, "E999", Severity.CRITICAL),
    ])
    assert [e["file"] for e in excerpts] == ["broken.py", "minor.py"]


# ----------------------------------------------------------- problem status --


def test_status_follows_a_line_through_the_diff():
    old = "import os\nif x == None:\n\tpass\n"
    new = "if x == None:\n    pass\n"  # import removed above, so lines shift up
    before = [_problem("a.py", 1, "F401"), _problem("a.py", 2, "E711")]
    after = [_problem("a.py", 1, "E711")]  # E711 still there, now on line 1
    status = problem_status(before, after, {"a.py": old}, {"a.py": new}, {"a.py"})
    assert status == {"a.py:1:F401": "resolved", "a.py:2:E711": "remaining"}


def test_a_problem_in_an_untouched_file_can_still_be_resolved():
    # e.g. an undeclared import fixed by editing requirements.txt elsewhere
    before = [_problem("app.py", 3, "UNDECLARED-IMPORT")]
    status = problem_status(before, [], {}, {}, set())
    assert status == {"app.py:3:UNDECLARED-IMPORT": "resolved"}


def test_a_problem_still_on_an_untouched_line_remains():
    before = [_problem("app.py", 3, "E711")]
    status = problem_status(before, [_problem("app.py", 3, "E711")], {}, {}, set())
    assert status == {"app.py:3:E711": "remaining"}


def test_views_come_from_git_when_no_copy_was_taken(tmp_path):
    (tmp_path / "a.py").write_text("x = 2\n")
    views = build_code_views(tmp_path, ["a.py"], {}, [], [])
    assert views["diffs"][0]["original_known"] is False  # no .git here either


# ------------------------------------------------------------------ pipeline --


def test_a_run_publishes_excerpts_diffs_and_statuses(broken_repo, tmp_path, monkeypatch):
    job = _run(broken_repo, tmp_path, monkeypatch, push=False)
    snapshot = job.snapshot()

    excerpt_files = {e["file"] for e in snapshot["excerpts"]}
    assert "src/pkg/calc.py" in excerpt_files

    diffs = {d["file"]: d for d in snapshot["diffs"]}
    assert "package.json" in diffs, "the JSON repair shows as a diff"
    rows = [r for hunk in diffs["package.json"]["hunks"] for r in hunk["rows"]]
    assert any(r["type"] == "del" for r in rows) and any(r["type"] == "add" for r in rows)
    assert all(d["original_known"] for d in snapshot["diffs"])

    keys = {f"{p['file']}:{p['line']}:{p['code']}" for p in snapshot["problems"]}
    assert set(snapshot["problem_status"]) == keys, "every problem gets a status"
    assert "resolved" in snapshot["problem_status"].values()
