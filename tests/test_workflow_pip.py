"""pip commands inside GitHub Actions job steps.

Workflow shape checks pass on a job whose very first command fails, so the
step bodies are inspected too. These tests pin both halves: the defects are
found, and ordinary workflows stay quiet.
"""

from pathlib import Path

import pytest
from healing_agent.analysis.scanning import build_inventory
from healing_agent.analysis.workflow_steps import detect_pip_problems
from healing_agent.models import Severity


def _workflow(tmp_path: Path, body: str, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(body)
    for name, content in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _codes(root: Path) -> set[str]:
    return {p.code for p in detect_pip_problems(build_inventory(root))}


HEADER = "name: CI\non: [push]\njobs:\n"


def _job(name: str, steps: str, python: str | None = "3.12") -> str:
    setup = ""
    if python is not None:
        setup = (
            "      - uses: actions/setup-python@v5\n"
            "        with:\n"
            f'          python-version: "{python}"\n'
        )
    return f"  {name}:\n    runs-on: ubuntu-latest\n    steps:\n{setup}{steps}"


def test_missing_requirements_file_is_critical(tmp_path):
    root = _workflow(
        tmp_path,
        HEADER + _job("build", "      - run: python -m pip install -r dev.txt\n"),
    )
    problems = detect_pip_problems(build_inventory(root))
    assert [p.code for p in problems] == ["PIP-MISSING-REQUIREMENTS"]
    assert problems[0].severity is Severity.CRITICAL
    assert "dev.txt" in problems[0].message


def test_present_requirements_file_is_fine(tmp_path):
    root = _workflow(
        tmp_path,
        HEADER + _job("build", "      - run: python -m pip install -r reqs.txt\n"),
        files={"reqs.txt": "requests\n"},
    )
    assert _codes(root) == set()


def test_requirements_in_a_subdirectory_resolves(tmp_path):
    root = _workflow(
        tmp_path,
        HEADER
        + _job("build", "      - run: python -m pip install -r backend/reqs.txt\n"),
        files={"backend/reqs.txt": "requests\n"},
    )
    assert _codes(root) == set()


def test_exact_pins_against_floating_python_are_flagged(tmp_path):
    """The wheel-availability trap: pinned builds, unpinned interpreter."""
    root = _workflow(
        tmp_path,
        HEADER
        + _job(
            "build",
            "      - run: python -m pip install pydantic==2.10.4\n",
            python="3.x",
        ),
    )
    assert "PIP-PINNED-FLOATING-PYTHON" in _codes(root)


def test_exact_pins_with_a_pinned_python_are_fine(tmp_path):
    root = _workflow(
        tmp_path,
        HEADER
        + _job(
            "build",
            "      - run: python -m pip install pydantic==2.10.4\n",
            python="3.11",
        ),
    )
    assert "PIP-PINNED-FLOATING-PYTHON" not in _codes(root)


def test_bare_pip_executable_is_flagged(tmp_path):
    root = _workflow(
        tmp_path, HEADER + _job("build", "      - run: pip install ruff\n")
    )
    assert "PIP-BARE-EXECUTABLE" in _codes(root)


def test_python_dash_m_pip_is_not_flagged(tmp_path):
    root = _workflow(
        tmp_path, HEADER + _job("build", "      - run: python -m pip install ruff\n")
    )
    assert "PIP-BARE-EXECUTABLE" not in _codes(root)


def test_sudo_pip_is_flagged(tmp_path):
    root = _workflow(
        tmp_path, HEADER + _job("build", "      - run: sudo pip install ruff\n")
    )
    assert "PIP-SUDO" in _codes(root)


@pytest.mark.parametrize(
    "step",
    [
        "      - run: python -m pip install -r ${{ env.REQS }}\n",
        "      - run: npm ci && npm test\n",
        "      - uses: actions/checkout@v4\n",
    ],
)
def test_no_false_positives(tmp_path, step):
    """Expressions, non-pip steps, and action steps must stay quiet."""
    root = _workflow(tmp_path, HEADER + _job("build", step))
    assert _codes(root) == set()


def test_multiline_run_blocks_are_inspected(tmp_path):
    root = _workflow(
        tmp_path,
        HEADER
        + _job(
            "build",
            "      - run: |\n"
            "          python -m pip install --upgrade pip\n"
            "          python -m pip install -r absent.txt\n",
        ),
    )
    assert "PIP-MISSING-REQUIREMENTS" in _codes(root)


def test_findings_point_at_the_offending_line(tmp_path):
    body = (
        HEADER
        + _job(
            "build",
            "      - run: |\n"
            "          echo one\n"
            "          echo two\n"
            "          python -m pip install -r absent.txt\n",
        )
    )
    root = _workflow(tmp_path, body)
    problem = detect_pip_problems(build_inventory(root))[0]
    reported = body.splitlines()[problem.line - 1]
    assert "absent.txt" in reported, f"line {problem.line} was {reported!r}"


def test_unparseable_workflow_is_left_to_the_yaml_detector(tmp_path):
    root = _workflow(tmp_path, "name: CI\njobs:\n  a:\n   - bad\n  indent\n")
    assert _codes(root) == set()
