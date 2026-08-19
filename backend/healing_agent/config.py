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


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


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

    anthropic_api_key: str | None = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY") or None
    )
    model: str = field(
        default_factory=lambda: os.environ.get("HEALING_AGENT_MODEL", "claude-opus-5")
    )
    effort: str = field(
        default_factory=lambda: os.environ.get("HEALING_AGENT_EFFORT", "high")
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

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def git_cli_available(self) -> bool:
        return shutil.which("git") is not None

    def describe(self) -> dict[str, object]:
        """Non-secret view of the configuration, safe to serve over HTTP."""
        return {
            "model": self.model,
            "effort": self.effort,
            "ai_enabled": self.ai_enabled,
            "git_cli_available": self.git_cli_available,
            "repo_backend": "git-cli" if self.git_cli_available else "github-api",
            "workspace_root": str(self.workspace_root),
            "max_rounds": self.max_rounds,
            "job_timeout_seconds": self.job_timeout_seconds,
            "status_page_url": self.status_page_url,
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
