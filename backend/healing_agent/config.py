"""Runtime configuration, resolved from environment variables."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    # "none" means allow no cross-origin requests at all. An unset variable
    # falls back to the default, so without this there would be no way to
    # express "lock it down" -- which is what a single-origin deployment,
    # where the dashboard is served by this same process, actually wants.
    if raw.strip().lower() == "none":
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_env_file(path: Path | str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Real environment variables win: a value already set is never overwritten.
    Called by the entrypoints (run.py, scripts), not at import, so tests never
    pick up a developer's local credentials.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _default_workspace() -> Path:
    """Pick a writable workspace root.

    Serverless runtimes (Vercel, Lambda) ship a read-only filesystem with the
    single exception of /tmp, so fall back there when the configured directory
    cannot be created.
    """
    configured = os.environ.get("HEALING_AGENT_WORKSPACE", ".workspaces")
    candidate = Path(configured).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-probe"
        probe.touch()
        probe.unlink()
        return candidate.resolve()
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "healing-agent-workspaces"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback.resolve()


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the agent's configuration."""

    # The AI repair model. The model name is configurable because Google
    # retires model ids on its own schedule.
    gemini_api_key: str | None = field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or None
    )
    gemini_model: str = field(
        default_factory=lambda: os.environ.get(
            "HEALING_AGENT_GEMINI_MODEL", "gemini-2.5-flash"
        )
    )
    # How long a run waits for the repository's real CI on the healing commit
    # (only when verify_in_ci is requested). Keep it under the job timeout.
    ci_wait_seconds: int = field(
        default_factory=lambda: max(30, _env_int("HEALING_AGENT_CI_WAIT_SECONDS", 300))
    )
    # How often the background monitor re-verifies each configured model.
    model_check_seconds: int = field(
        default_factory=lambda: max(
            30, _env_int("HEALING_AGENT_MODEL_CHECK_SECONDS", 300)
        )
    )
    fallback_github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or None
    )
    workspace_root: Path = field(default_factory=_default_workspace)
    cors_origins: list[str] = field(
        default_factory=lambda: _env_list("HEALING_AGENT_CORS_ORIGINS", ["*"])
    )
    max_rounds: int = field(
        default_factory=lambda: max(1, _env_int("HEALING_AGENT_MAX_ROUNDS", 3))
    )
    job_timeout_seconds: int = field(
        default_factory=lambda: _env_int("HEALING_AGENT_JOB_TIMEOUT", 900)
    )
    max_ai_files: int = field(
        default_factory=lambda: _env_int("HEALING_AGENT_MAX_AI_FILES", 40)
    )
    status_page_url: str = field(
        default_factory=lambda: os.environ.get(
            "HEALING_AGENT_STATUS_URL",
            "https://www.githubstatus.com/api/v2/summary.json",
        )
    )
    max_file_bytes: int = field(
        default_factory=lambda: _env_int("HEALING_AGENT_MAX_FILE_BYTES", 400_000)
    )
    max_repo_files: int = field(
        default_factory=lambda: _env_int("HEALING_AGENT_MAX_REPO_FILES", 4000)
    )
    # Running a cloned repository's test suite executes code from that
    # repository: pytest imports conftest.py and every test module at
    # collection time. That is fine when you point the agent at your own code,
    # and unacceptable on a shared deployment where anyone can submit a URL.
    # Off unless an operator explicitly turns it on.
    allow_test_execution: bool = field(
        default_factory=lambda: _env_bool("HEALING_AGENT_ALLOW_TEST_EXECUTION", False)
    )

    @property
    def ai_enabled(self) -> bool:
        """True when the Gemini key is configured."""
        return bool(self.gemini_api_key)

    @property
    def git_cli_available(self) -> bool:
        return shutil.which("git") is not None

    def describe(self) -> dict[str, object]:
        """Non-secret view of the configuration, safe to serve over HTTP."""
        return {
            "ai_enabled": self.ai_enabled,
            "model": self.gemini_model,
            "model_check_seconds": self.model_check_seconds,
            "ci_wait_seconds": self.ci_wait_seconds,
            "git_cli_available": self.git_cli_available,
            "repo_backend": "git-cli" if self.git_cli_available else "github-api",
            "workspace_root": str(self.workspace_root),
            "max_rounds": self.max_rounds,
            "job_timeout_seconds": self.job_timeout_seconds,
            "status_page_url": self.status_page_url,
            "test_execution_allowed": self.allow_test_execution,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that patch the environment."""
    global _settings
    _settings = None
