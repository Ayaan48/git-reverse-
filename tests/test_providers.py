"""AI providers (Claude, Gemini): fallback, failure handling, and live model checks."""

import json
import logging

import httpx
import pytest
from healing_agent.config import Settings
from healing_agent.healing import ai as ai_module
from healing_agent.healing import providers as prov
from healing_agent.healing.providers import (
    AnthropicProvider,
    GeminiProvider,
    ModelStatus,
    ProviderError,
    RepairProvider,
    _gemini_schema,
    active_provider_names,
    build_providers,
    cached_model_status,
    check_models,
)
from healing_agent.models import Problem, ProblemKind, Severity

GEMINI_KEY = "AIzaSyFAKE-KEY-FOR-TESTS-0123456789abcd"


@pytest.fixture(autouse=True)
def _fresh_cache():
    prov.reset_model_cache()
    yield
    prov.reset_model_cache()


def _settings(tmp_path, **kw):
    kw.setdefault("anthropic_api_key", None)
    kw.setdefault("gemini_api_key", None)
    return Settings(workspace_root=tmp_path, **kw)


def _problem(rel="m.py"):
    return Problem(
        file=rel, line=2, column=12, kind=ProblemKind.TYPE, severity=Severity.CRITICAL,
        code="F821", message="Undefined name `statistics`", detector="ruff",
    )


def _gemini_answer(payload, finish="STOP"):
    return {"candidates": [{
        "content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": finish,
    }]}


FIXED = {
    "fixed_content": "import statistics\n\n\ndef mean(v):\n    return statistics.mean(v)\n",
    "changes": ["Added the missing import"], "unable_to_fix": False, "reason": "",
}
BROKEN_SRC = "def mean(v):\n    return statistics.mean(v)\n"


def _gemini(tmp_path, handler, **kw):
    settings = _settings(tmp_path, gemini_api_key=GEMINI_KEY, **kw)
    return GeminiProvider(
        settings, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )


# ------------------------------------------------------------------ schema --


def test_gemini_schema_uses_its_dialect():
    out = _gemini_schema(ai_module.RESPONSE_SCHEMA)
    text = json.dumps(out)
    assert out["type"] == "OBJECT"
    assert out["properties"]["changes"]["items"]["type"] == "STRING"
    assert "additionalProperties" not in text
    assert out["required"] == ai_module.RESPONSE_SCHEMA["required"]


# ------------------------------------------------------------------ gemini --


def test_gemini_request_shape_and_key_stays_out_of_the_url(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_gemini_answer(FIXED))

    provider = _gemini(tmp_path, handler)
    payload = provider.request_repair("m.py", BROKEN_SRC, [_problem()], "hint text")

    assert payload["fixed_content"].startswith("import statistics")
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert GEMINI_KEY not in seen["url"], "key must travel in a header, not the URL"
    assert seen["key"] == GEMINI_KEY
    config = seen["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["type"] == "OBJECT"
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    assert "hint text" in prompt and "Undefined name `statistics`" in prompt
    assert seen["body"]["systemInstruction"]["parts"][0]["text"]


def test_gemini_ignores_thought_parts(tmp_path):
    answer = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "let me think...", "thought": True},
        {"text": json.dumps(FIXED)},
    ]}}]}
    provider = _gemini(tmp_path, lambda r: httpx.Response(200, json=answer))
    assert provider.request_repair("m.py", BROKEN_SRC, [_problem()])["changes"]


@pytest.mark.parametrize("status, message", [
    (401, "API key not valid"),
    (403, "Permission denied"),
    (404, "models/gemini-9 is not found"),
    (400, "API key not valid. Please pass a valid API key."),
])
def test_gemini_auth_and_missing_model_are_fatal(tmp_path, status, message):
    provider = _gemini(
        tmp_path,
        lambda r: httpx.Response(status, json={"error": {"message": message}}),
    )
    with pytest.raises(ProviderError) as info:
        provider.request_repair("m.py", BROKEN_SRC, [_problem()])
    assert info.value.fatal


def test_gemini_rate_limit_retries_once_then_is_fatal(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

    provider = _gemini(tmp_path, handler)
    with pytest.raises(ProviderError) as info:
        provider.request_repair("m.py", BROKEN_SRC, [_problem()])
    assert info.value.fatal and len(calls) == 2


def test_gemini_transient_5xx_recovers(tmp_path):
    responses = [httpx.Response(503, json={"error": {"message": "overloaded"}}),
                 httpx.Response(200, json=_gemini_answer(FIXED))]
    provider = _gemini(tmp_path, lambda r: responses.pop(0))
    assert provider.request_repair("m.py", BROKEN_SRC, [_problem()])["changes"]


def test_gemini_persistent_5xx_is_not_fatal(tmp_path):
    provider = _gemini(
        tmp_path, lambda r: httpx.Response(500, json={"error": {"message": "boom"}})
    )
    with pytest.raises(ProviderError) as info:
        provider.request_repair("m.py", BROKEN_SRC, [_problem()])
    assert not info.value.fatal


@pytest.mark.parametrize("answer, expected", [
    (_gemini_answer(FIXED, finish="MAX_TOKENS"), "truncated"),
    (_gemini_answer(FIXED, finish="SAFETY"), "declined"),
    ({"candidates": [], "promptFeedback": {"blockReason": "OTHER"}}, "blocked"),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "{not json"}]}}]}, "invalid JSON"),
])
def test_gemini_bad_answers_are_non_fatal_errors(tmp_path, answer, expected):
    provider = _gemini(tmp_path, lambda r: httpx.Response(200, json=answer))
    with pytest.raises(ProviderError, match=expected) as info:
        provider.request_repair("m.py", BROKEN_SRC, [_problem()])
    assert not info.value.fatal


def test_gemini_check_reports_ok_and_failure(tmp_path):
    ok = _gemini(tmp_path, lambda r: httpx.Response(200, json=_gemini_answer({})))
    assert ok.check().ok is True

    bad = _gemini(
        tmp_path,
        lambda r: httpx.Response(403, json={"error": {"message": "key revoked"}}),
    )
    status = bad.check()
    assert status.ok is False and "key revoked" in status.detail
    assert GEMINI_KEY not in status.detail


# ------------------------------------------------------------------ claude --


class _Err(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_claude_credit_error_is_fatal(tmp_path):
    exc = _Err("Your credit balance is too low to access the Anthropic API.", 400)
    assert AnthropicProvider._classify(exc).fatal
    assert AnthropicProvider._classify(_Err("invalid x-api-key", 401)).fatal
    assert not AnthropicProvider._classify(_Err("overloaded", 529)).fatal


def test_claude_check_surfaces_the_billing_error(tmp_path):
    class Client:
        def with_options(self, **kw):
            return self

        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                raise _Err("Your credit balance is too low to access the API", 400)

    provider = AnthropicProvider(_settings(tmp_path, anthropic_api_key="sk-ant-x"), client=Client())
    status = provider.check()
    assert status.ok is False and "credit balance" in status.detail


# ---------------------------------------------------------------- registry --


def test_provider_order_and_selection(tmp_path):
    both = _settings(tmp_path, anthropic_api_key="a", gemini_api_key="g")
    assert active_provider_names(both) == ["anthropic", "gemini"]
    assert [p.name for p in build_providers(both)] == ["anthropic", "gemini"]

    only_gemini = _settings(tmp_path, anthropic_api_key="a", gemini_api_key="g",
                            ai_provider="gemini")
    assert active_provider_names(only_gemini) == ["gemini"]

    asked_but_unkeyed = _settings(tmp_path, anthropic_api_key="a", ai_provider="gemini")
    assert active_provider_names(asked_but_unkeyed) == []
    assert _settings(tmp_path).ai_enabled is False
    assert _settings(tmp_path, gemini_api_key="g").ai_enabled is True


# ---------------------------------------------------------- repair fallback --


class FakeProvider(RepairProvider):
    def __init__(self, name, behaviour):
        self.name, self.model = name, f"{name}-model"
        self.behaviour, self.calls = behaviour, 0

    def request_repair(self, rel, source, problems, memory_context=""):
        self.calls += 1
        result = self.behaviour
        if isinstance(result, Exception):
            raise result
        return result

    def check(self):
        return ModelStatus(self.name, self.model, True, True, "ok")


def _repo(tmp_path, count=1):
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(count):
        (root / f"m{i}.py").write_text(BROKEN_SRC)
    return root


def _scan(root):
    from healing_agent.analysis import scan_repository

    return scan_repository(root).problems


def _use(monkeypatch, *providers):
    monkeypatch.setattr(ai_module, "build_providers", lambda settings: list(providers))


def test_falls_back_to_gemini_when_claude_has_no_credit(tmp_path, monkeypatch):
    root = _repo(tmp_path, count=3)
    claude = FakeProvider("anthropic", ProviderError("credit balance is too low", fatal=True))
    gemini = FakeProvider("gemini", FIXED)
    _use(monkeypatch, claude, gemini)

    outcome = ai_module.apply_ai_fixes(root, _scan(root), _settings(tmp_path, gemini_api_key="g"))

    assert outcome.accepted == 3 and outcome.rejected == 0
    assert claude.calls == 1, "a fatally failed provider is retired for the run"
    assert gemini.calls == 3
    assert outcome.models_used == {"gemini-model": 3}
    assert any("anthropic" in n and "unavailable" in n for n in outcome.notes)
    assert "via gemini-model" in outcome.fixes[0].description
    assert (root / "m0.py").read_text().startswith("import statistics")


def test_non_fatal_failure_falls_through_without_retiring_the_provider(tmp_path, monkeypatch):
    root = _repo(tmp_path, count=2)
    claude = FakeProvider("anthropic", ProviderError("response truncated"))
    gemini = FakeProvider("gemini", FIXED)
    _use(monkeypatch, claude, gemini)

    ai_module.apply_ai_fixes(root, _scan(root), _settings(tmp_path, gemini_api_key="g"))
    assert claude.calls == 2, "a one-off failure must not retire the provider"


def test_all_providers_dead_stops_early_and_changes_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path, count=3)
    dead = ProviderError("credit balance is too low", fatal=True)
    claude = FakeProvider("anthropic", dead)
    gemini = FakeProvider("gemini", ProviderError("quota exceeded", fatal=True))
    _use(monkeypatch, claude, gemini)

    outcome = ai_module.apply_ai_fixes(root, _scan(root), _settings(tmp_path, gemini_api_key="g"))

    assert outcome.accepted == 0 and not outcome.fixes
    assert claude.calls == 1 and gemini.calls == 1, "no repeated calls to dead providers"
    assert any("Every AI provider is unavailable" in n for n in outcome.notes)
    assert (root / "m0.py").read_text() == BROKEN_SRC


def test_no_provider_configured_skips_the_tier(tmp_path):
    root = _repo(tmp_path)
    outcome = ai_module.apply_ai_fixes(root, _scan(root), _settings(tmp_path))
    assert "No AI provider is configured" in outcome.skipped_reason


def test_gemini_output_faces_the_same_verification_gate(tmp_path, monkeypatch):
    """A Gemini rewrite that deletes the function is rejected, like any other."""
    root = _repo(tmp_path)
    deleting = {"fixed_content": "import statistics\n", "changes": ["tidy"],
                "unable_to_fix": False, "reason": ""}
    _use(monkeypatch, FakeProvider("gemini", deleting))

    outcome = ai_module.apply_ai_fixes(root, _scan(root), _settings(tmp_path, gemini_api_key="g"))

    assert outcome.accepted == 0 and outcome.rejected == 1
    assert (root / "m0.py").read_text() == BROKEN_SRC, "original must be restored"


# ----------------------------------------------------------------- monitor --


def test_check_models_logs_state_changes_once(tmp_path, caplog):
    results = [ModelStatus("gemini", "g", True, False, "key revoked"),
               ModelStatus("gemini", "g", True, False, "key revoked"),
               ModelStatus("gemini", "g", True, True, "responding")]

    class Probe(FakeProvider):
        def check(self):
            return results.pop(0)

    probe = Probe("gemini", None)
    settings = _settings(tmp_path, gemini_api_key="g")
    with caplog.at_level(logging.INFO, logger=prov.log.name):
        for _ in range(3):
            check_models(settings, providers=[probe])

    messages = [r.getMessage() for r in caplog.records if "AI model" in r.getMessage()]
    assert len(messages) == 2, "log on first sight and on each change, not every poll"
    assert "FAILING" in messages[0] and "OK" in messages[1]


def test_cached_status_avoids_calling_the_model_every_time(tmp_path, monkeypatch):
    calls = []

    def fake_check(settings, providers=None):
        calls.append(1)
        status = ModelStatus("gemini", "g", True, True, "responding", checked_at=prov.time.time())
        with prov._cache_lock:
            prov._cache["gemini"] = status
        return [status]

    monkeypatch.setattr(prov, "check_models", fake_check)
    settings = _settings(tmp_path, gemini_api_key="g", model_check_seconds=300)

    for _ in range(5):
        rows = cached_model_status(settings)
    assert len(calls) == 1
    assert any(r.provider == "anthropic" and not r.configured for r in rows)


def test_health_endpoint_reports_models_without_keys(tmp_path):
    from fastapi.testclient import TestClient
    from healing_agent.app import app

    body = TestClient(app).get("/api/health").json()
    models = {m["provider"]: m for m in body["checks"]["ai_models"]}
    assert set(models) == {"anthropic", "gemini"}
    assert all(m["configured"] is False and m["ok"] is None for m in models.values())
    assert body["checks"]["ai_repair_tier"] is False


# --------------------------------------------------------- docstring guard --


def test_gate_rejects_a_rewrite_that_drops_the_module_docstring():
    source = '"""Stock store."""\n\n\ndef f(x):\n    if x == None:\n        return 1\n    return 2\n'
    candidate = "def f(x):\n    if x is None:\n        return 1\n    return 2\n"
    reason = ai_module.rewrite_destroys_code(source, candidate, ".py")
    assert reason and "<module>" in reason


def test_gate_rejects_dropped_function_and_method_docstrings():
    source = (
        "class A:\n    def m(self):\n        '''doc'''\n        return 1\n\n\n"
        "def g():\n    '''doc'''\n    return 2\n"
    )
    candidate = "class A:\n    def m(self):\n        return 1\n\n\ndef g():\n    return 2\n"
    reason = ai_module.rewrite_destroys_code(source, candidate, ".py")
    assert reason and "A.m" in reason and "g" in reason


def test_gate_allows_a_rewrite_that_keeps_docstrings():
    source = '"""Doc."""\n\n\ndef f(x):\n    """Doc."""\n    if x == None:\n        return 1\n    return 2\n'
    candidate = source.replace("== None", "is None")
    assert ai_module.rewrite_destroys_code(source, candidate, ".py") is None


def test_docstring_check_ignores_files_that_already_fail_to_parse():
    # A file with a syntax error has no docstrings to lose; it must not be
    # rejected for that, or syntax errors could never be repaired.
    broken = "def f(:\n    '''doc'''\n"
    fixed = "def f():\n    '''doc'''\n"
    assert ai_module.rewrite_destroys_code(broken, fixed, ".py") is None
