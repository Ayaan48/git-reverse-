"""AI repair providers (Claude, Gemini) and live model health checks.

Both providers answer the same question -- "return the corrected file as JSON"
-- and the same verification gate judges the result, so the agent can use
whichever is available and fall back to the other without any change in how
much a repair is trusted.

Failures are classified, because they call for different responses:

* **fatal** -- the provider cannot help for the rest of this run (no credit,
  bad key, retired model, quota exhausted). Calling it again per file would just
  repeat the same failure, so the run stops using it and falls through.
* **non-fatal** -- this one request failed (refusal, truncated or malformed
  output, a transient 5xx). Other files may still succeed.

Gemini is called over plain HTTPS with httpx, which this project already
depends on, so enabling it adds no new dependency.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..models import Problem
from ..redaction import register_secret, scrub
from .prompts import RESPONSE_SCHEMA, SYSTEM_PROMPT, build_user_message

log = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
CHECK_TIMEOUT_SECONDS = 20.0
# Longest wait honoured for a per-minute rate limit before giving up.
MAX_RATE_LIMIT_WAIT = 60.0


class ProviderError(Exception):
    """A provider call failed. `fatal` means stop using it for this run."""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _clean(exc: BaseException, limit: int = 220) -> str:
    """Scrubbed, one-line error text. Timeouts often stringify to ''.

    SDK errors carry the provider's readable message in `.body`; prefer that
    over the raw `Error code: 400 - {...}` dump.
    """
    raw = str(exc)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        message = inner.get("message") if isinstance(inner, dict) else body.get("message")
        if isinstance(message, str) and message:
            raw = message
    text = " ".join(scrub(raw).split())
    return (text or type(exc).__name__)[:limit]


def _defect_lines(problems: list[Problem]) -> str:
    return "\n".join(
        f"- line {p.line}, {p.code} ({p.severity.value}): {p.message}"
        for p in problems
    )


# ----------------------------------------------------------------- status --


@dataclass
class ModelStatus:
    """Result of a live check of one provider's model."""

    provider: str
    model: str
    configured: bool
    ok: bool | None = None  # None = not configured, never checked
    detail: str = ""
    checked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "configured": self.configured,
            "ok": self.ok,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


# -------------------------------------------------------------- interface --


class RepairProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def request_repair(
        self,
        rel: str,
        source: str,
        problems: list[Problem],
        memory_context: str = "",
    ) -> dict[str, Any] | None:
        """Return the parsed repair payload, or None if the model said nothing."""

    @abstractmethod
    def check(self) -> ModelStatus:
        """Cheap live call proving the key, model, and billing all work."""


# ------------------------------------------------------------------ claude --


class AnthropicProvider(RepairProvider):
    name = "anthropic"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self.model = settings.model
        self._client = client
        register_secret(settings.anthropic_api_key)

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    @staticmethod
    def _classify(exc: Exception) -> ProviderError:
        status = getattr(exc, "status_code", None)
        text = _clean(exc)
        lowered = text.lower()
        fatal = (
            status in (401, 403, 404)
            or "credit balance" in lowered
            or "billing" in lowered
            or "authentication" in lowered
        )
        return ProviderError(text, fatal=fatal)

    def request_repair(self, rel, source, problems, memory_context=""):
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 64000,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": build_user_message(
                        rel, source, _defect_lines(problems), memory_context
                    ),
                }
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        }
        try:
            # Streaming keeps a large max_tokens from tripping the HTTP timeout.
            with self.client.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 - classified below
            raise self._classify(exc) from exc

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            raise ProviderError(
                f"model declined to repair this file "
                f"({getattr(details, 'category', 'unspecified')})"
            )
        text = next(
            (block.text for block in message.content if block.type == "text"), None
        )
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"model returned invalid JSON: {exc}") from exc

    def check(self) -> ModelStatus:
        status = ModelStatus("anthropic", self.model, configured=True)
        try:
            self.client.with_options(timeout=CHECK_TIMEOUT_SECONDS).messages.create(
                model=self.model,
                max_tokens=8,
                messages=[{"role": "user", "content": "Reply with OK."}],
            )
            status.ok, status.detail = True, "responding"
        except Exception as exc:  # noqa: BLE001
            status.ok, status.detail = False, _clean(exc)
        status.checked_at = time.time()
        return status


# ------------------------------------------------------------------ gemini --


def _gemini_schema(schema: Any) -> Any:
    """Translate the JSON Schema into Gemini's OpenAPI-subset dialect.

    Gemini wants upper-case type names and rejects `additionalProperties`.
    """
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, str):
            out[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: _gemini_schema(sub) for name, sub in value.items()}
        else:
            out[key] = _gemini_schema(value)
    return out


_GEMINI_DECLINED = {
    "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII",
    "IMAGE_SAFETY", "LANGUAGE",
}


class GeminiProvider(RepairProvider):
    name = "gemini"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.model = settings.gemini_model
        self._key = settings.gemini_api_key or ""
        register_secret(self._key)
        self._sleep = sleep
        # The key travels in a header, never the URL, so it cannot surface in
        # an httpx error message that quotes the request URL.
        self._http = httpx.Client(
            base_url=GEMINI_BASE_URL,
            headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(180.0, connect=15.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # -- transport -----------------------------------------------------------
    def _classify_http(self, response: httpx.Response) -> ProviderError:
        try:
            message = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            message = response.text
        text = f"HTTP {response.status_code}: {_clean(Exception(message))}"
        lowered = message.lower()
        status = response.status_code
        fatal = (
            status in (401, 403, 404)
            or (status == 400 and ("api key" in lowered or "api_key" in lowered))
            or status == 429
        )
        return ProviderError(text, fatal=fatal)

    def _post(self, body: dict[str, Any], timeout: float | None = None) -> dict:
        url = f"/models/{self.model}:generateContent"
        last: ProviderError | None = None
        for attempt in range(3):
            try:
                response = self._http.post(url, json=body, timeout=timeout)
            except httpx.HTTPError as exc:
                last = ProviderError(f"network error: {_clean(exc)}")
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderError("Gemini returned a non-JSON body") from exc
                last = self._classify_http(response)
                if response.status_code == 429:
                    # A per-minute limit clears on its own: wait as long as
                    # Google asks, once. A daily quota will not clear today.
                    wait = self._retry_after(response)
                    if attempt == 0 and wait is not None and wait <= MAX_RATE_LIMIT_WAIT:
                        self._sleep(wait)
                        continue
                    raise last
                if response.status_code < 500:
                    raise last
            if attempt < 2:
                self._sleep(2.0 * (attempt + 1))
        assert last is not None
        raise last

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Seconds Google asks us to wait, or None for a quota that won't
        clear soon (a per-day limit)."""
        try:
            details = (response.json().get("error") or {}).get("details") or []
        except ValueError:
            return 2.0
        for detail in details:
            for violation in detail.get("violations") or []:
                if "PerDay" in str(violation.get("quotaId") or ""):
                    return None
        for detail in details:
            delay = detail.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return max(1.0, float(delay[:-1]))
                except ValueError:
                    pass
        return 2.0

    # -- interface -----------------------------------------------------------
    def request_repair(self, rel, source, problems, memory_context=""):
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": build_user_message(
                                rel, source, _defect_lines(problems), memory_context
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _gemini_schema(RESPONSE_SCHEMA),
                "maxOutputTokens": 65536,
                "temperature": 0,
            },
        }
        data = self._post(body)

        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason", "unknown")
            raise ProviderError(f"Gemini returned no answer (blocked: {blocked})")
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        if reason in _GEMINI_DECLINED:
            raise ProviderError(f"model declined to repair this file ({reason})")
        if reason == "MAX_TOKENS":
            raise ProviderError("response was truncated (MAX_TOKENS)")

        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(
            part.get("text", "") for part in parts if not part.get("thought")
        )
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"model returned invalid JSON: {exc}") from exc

    def check(self) -> ModelStatus:
        """Confirm the key works and the model exists, without generating.

        The free tier's per-minute limit is small. A test generation here
        would compete with real repairs for it, so the monitor asks for the
        model's metadata instead, which proves the key and the model id
        without spending any generation quota. A quota problem still shows
        up when a repair runs, and the run falls back to the other model.
        """
        status = ModelStatus("gemini", self.model, configured=True)
        try:
            response = self._http.get(f"/models/{self.model}", timeout=CHECK_TIMEOUT_SECONDS)
            if response.status_code == 200:
                status.ok, status.detail = True, "key and model OK"
            else:
                status.ok, status.detail = False, str(self._classify_http(response))
        except Exception as exc:  # noqa: BLE001
            status.ok, status.detail = False, _clean(exc)
        status.checked_at = time.time()
        return status


# ---------------------------------------------------------------- registry --


def active_provider_names(settings: Settings) -> list[str]:
    """Providers in play, in order: selected by HEALING_AGENT_PROVIDER AND keyed."""
    order = {"anthropic": ["anthropic"], "gemini": ["gemini"]}.get(
        settings.ai_provider, ["anthropic", "gemini"]
    )
    keys = {"anthropic": settings.anthropic_api_key, "gemini": settings.gemini_api_key}
    return [name for name in order if keys[name]]


def build_providers(settings: Settings) -> list[RepairProvider]:
    """Providers to try, in order."""
    return [
        AnthropicProvider(settings) if name == "anthropic" else GeminiProvider(settings)
        for name in active_provider_names(settings)
    ]


# ------------------------------------------------------------ model monitor --

_cache: dict[str, ModelStatus] = {}
_cache_lock = threading.Lock()


def _unconfigured(settings: Settings) -> list[ModelStatus]:
    """Rows for providers not in play, so the dashboard can say why."""
    active = set(active_provider_names(settings))
    rows = []
    for name, key, model, label in (
        ("anthropic", settings.anthropic_api_key, settings.model, "Anthropic"),
        ("gemini", settings.gemini_api_key, settings.gemini_model, "Gemini"),
    ):
        if name in active:
            continue
        reason = (
            f"no {label} API key configured"
            if not key
            else f"not selected (HEALING_AGENT_PROVIDER={settings.ai_provider})"
        )
        rows.append(ModelStatus(name, model, False, None, reason))
    return rows


def check_models(
    settings: Settings, providers: list[RepairProvider] | None = None
) -> list[ModelStatus]:
    """Live-check every configured provider and refresh the cache.

    Logs on every change of state so an outage (or a recovery) shows up in the
    server log the moment the monitor notices it, not when a run fails.
    """
    providers = build_providers(settings) if providers is None else providers
    results: list[ModelStatus] = []
    for provider in providers:
        status = provider.check()
        with _cache_lock:
            previous = _cache.get(provider.name)
            _cache[provider.name] = status
        if previous is None or previous.ok != status.ok:
            level = logging.INFO if status.ok else logging.WARNING
            log.log(
                level,
                "AI model %s (%s): %s - %s",
                provider.name,
                status.model,
                "OK" if status.ok else "FAILING",
                status.detail,
            )
        results.append(status)
        close = getattr(provider, "close", None)
        if close:
            close()
    return results + _unconfigured(settings)


def cached_model_status(settings: Settings) -> list[ModelStatus]:
    """Latest known status, refreshing it first when stale or missing."""
    configured = active_provider_names(settings)
    now = time.time()
    with _cache_lock:
        fresh = all(
            name in _cache
            and _cache[name].checked_at is not None
            and now - _cache[name].checked_at < settings.model_check_seconds
            for name in configured
        )
        snapshot = [_cache[name] for name in configured if name in _cache]
    if configured and not fresh:
        return check_models(settings)
    return snapshot + _unconfigured(settings)


def reset_model_cache() -> None:
    """Drop cached statuses. Used by tests."""
    with _cache_lock:
        _cache.clear()
