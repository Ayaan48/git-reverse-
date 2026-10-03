import React, { useCallback, useEffect, useRef, useState } from "react";
import AnalyzeForm from "./components/AnalyzeForm.jsx";
import Progress from "./components/Progress.jsx";
import StatTiles from "./components/StatTiles.jsx";
import Findings from "./components/Findings.jsx";
import Fixes from "./components/Fixes.jsx";
import Validation from "./components/Validation.jsx";
import Diagnosis from "./components/Diagnosis.jsx";
import Memory, { MemoryPill } from "./components/Memory.jsx";
import ScoreCard from "./components/ScoreCard.jsx";
import ActivityLog from "./components/ActivityLog.jsx";
import ResultPanel from "./components/ResultPanel.jsx";
import { PipelineHealth, RepoHealth } from "./components/HealthPanels.jsx";
import RecentRuns from "./components/RecentRuns.jsx";
import { getHealth, startAnalysis, subscribeToJob } from "./api.js";

const TERMINAL = ["succeeded", "partial", "failed", "cancelled"];

const PREFERS_REDUCED_MOTION = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/**
 * Reveal cards as they scroll into view.
 *
 * Re-runs whenever `dep` changes because panels appear mid-run as the job
 * streams events. Each element is unobserved once revealed, so a card that is
 * updating live never re-animates.
 */
function useScrollReveal(dep) {
  useEffect(() => {
    // Select by :not(.revealed), NOT :not(.reveal). This effect re-runs on
    // every streamed update and disconnects the previous observer; selecting
    // on .reveal would skip cards that were already marked but had not yet
    // scrolled into view, leaving them observed by nobody and invisible for
    // good.
    const cards = document.querySelectorAll(".card:not(.revealed)");
    if (!cards.length) return undefined;

    const revealAll = () =>
      document
        .querySelectorAll(".card.reveal:not(.revealed)")
        .forEach((el) => el.classList.add("revealed"));

    // Nothing may depend on JS to become visible at all. If motion is
    // unwanted or the observer is unavailable, show everything outright.
    if (PREFERS_REDUCED_MOTION() || !("IntersectionObserver" in window)) {
      cards.forEach((el) => el.classList.add("reveal", "revealed"));
      return undefined;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("revealed");
            observer.unobserve(entry.target);
          }
        });
      },
      // Positive bottom margin starts the reveal just before a card scrolls
      // into view, so it settles rather than popping.
      { threshold: 0.01, rootMargin: "0px 0px 12% 0px" },
    );

    cards.forEach((el, index) => {
      if (!el.classList.contains("reveal")) {
        el.classList.add("reveal");
        // Stagger only the first screenful; later cards should not feel laggy.
        el.style.setProperty("--reveal-delay", `${Math.min(index, 5) * 55}ms`);
      }
      observer.observe(el);
    });

    // Failsafe: whatever has not revealed after this point is shown anyway.
    // A decorative animation must never be the reason content stays hidden --
    // from a stalled observer, a print job, or a page capture.
    const failsafe = window.setTimeout(revealAll, 8000);
    window.addEventListener("beforeprint", revealAll);

    return () => {
      observer.disconnect();
      window.clearTimeout(failsafe);
      window.removeEventListener("beforeprint", revealAll);
    };
  }, [dep]);
}

/** Condense the sticky masthead once the page has scrolled past its height. */
function useCondensedHeader(threshold = 40) {
  const [condensed, setCondensed] = useState(false);
  useEffect(() => {
    let frame = 0;
    const onScroll = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        setCondensed(window.scrollY > threshold);
        frame = 0;
      });
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [threshold]);
  return condensed;
}

function useTheme() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem("healing-agent-theme") || "system",
  );
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    localStorage.setItem("healing-agent-theme", theme);
  }, [theme]);
  return [theme, setTheme];
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [job, setJob] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [theme, setTheme] = useTheme();

  const unsubscribeRef = useRef(null);
  const startedAtRef = useRef(null);

  const condensed = useCondensedHeader();
  // Keyed on the panels that can appear mid-run, so new cards get observed.
  useScrollReveal(
    `${job?.job_id ?? "idle"}:${job?.status ?? ""}:${job?.fixes_applied ?? 0}:` +
      `${job?.validations?.length ?? 0}:${job?.score?.total ?? ""}`,
  );

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  // Local ticker so the elapsed readout advances smoothly between events.
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => {
      if (startedAtRef.current) {
        setElapsed((Date.now() - startedAtRef.current) / 1000);
      }
    }, 100);
    return () => clearInterval(timer);
  }, [running]);

  useEffect(() => () => unsubscribeRef.current?.(), []);

  const applySnapshot = useCallback((snapshot) => {
    setJob(snapshot);
    // Server time, so a run opened part-way shows its real elapsed time.
    const started = snapshot.started_at ?? snapshot.created_at;
    if (typeof started === "number") startedAtRef.current = started * 1000;
    if (typeof snapshot.elapsed_seconds === "number" && TERMINAL.includes(snapshot.status)) {
      setElapsed(snapshot.elapsed_seconds);
    }
    if (TERMINAL.includes(snapshot.status)) {
      setRunning(false);
    }
  }, []);

  // Incremental events keep the UI live without refetching the whole snapshot.
  const applyEvent = useCallback((event) => {
    const { type, data } = event;
    setJob((current) => {
      if (!current) return current;
      const next = { ...current };
      switch (type) {
        case "log":
          next.logs = [...(current.logs ?? []), data];
          break;
        case "phase":
          next.phase = data.phase;
          next.progress = data.progress;
          next.status = data.status;
          break;
        case "progress":
          next.progress = data.progress;
          break;
        case "problems":
          next.problems = [...(current.problems ?? []), ...data.added];
          next.problems_found = data.total;
          next.problems_by_severity = data.by_severity;
          break;
        case "fix":
          next.fixes = [...(current.fixes ?? []), data.fix];
          next.fixes_applied = data.total_fixes;
          break;
        case "validation":
          next.validations = [...(current.validations ?? []), data];
          next.validation_passed = data.passed;
          break;
        case "diagnosis":
          next.diagnosis = data;
          break;
        case "remediation":
          next.remediations = [...(current.remediations ?? []), data];
          break;
        case "pipeline_health":
          next.pipeline_health = data;
          break;
        case "repo_health":
          next.repo_health = data;
          break;
        case "score":
          next.score = data;
          break;
        case "memory":
          next.memory = data;
          break;
        case "pull_request":
          next.pull_request_url = data.url;
          break;
        case "ci":
          next.ci_verification = data;
          break;
        default:
          return current;
      }
      return next;
    });
  }, []);

  const handleStart = useCallback(
    async (payload) => {
      setError(null);
      setJob(null);
      setElapsed(0);
      setRunning(true);
      startedAtRef.current = Date.now();
      unsubscribeRef.current?.();

      try {
        const accepted = await startAnalysis(payload);
        setJob({
          job_id: accepted.job_id,
          status: accepted.status,
          phase: "queued",
          progress: 0,
          branch_name: payload.branch_name,
          logs: [],
          problems: [],
          fixes: [],
          validations: [],
          remediations: [],
        });
        unsubscribeRef.current = subscribeToJob(accepted.job_id, {
          onSnapshot: applySnapshot,
          onEvent: applyEvent,
          onError: (streamError) => setError(streamError.message),
        });
      } catch (startError) {
        setError(startError.message);
        setRunning(false);
      }
    },
    [applyEvent, applySnapshot],
  );

  // Watch a run that was started elsewhere, e.g. by a CI-failure trigger.
  const openJob = useCallback(
    (jobId) => {
      setError(null);
      unsubscribeRef.current?.();
      setJob({ job_id: jobId, status: "running", phase: "queued", progress: 0, logs: [] });
      setRunning(true);
      unsubscribeRef.current = subscribeToJob(jobId, {
        onSnapshot: applySnapshot,
        onEvent: applyEvent,
        onError: (streamError) => setError(streamError.message),
      });
    },
    [applyEvent, applySnapshot],
  );

  const aiEnabled = health?.checks?.ai_repair_tier;
  // Live model checks: configured providers, and whether each one answers.
  const aiModels = (health?.checks?.ai_models ?? []).filter((m) => m.configured);
  const aiWorking = aiModels.some((m) => m.ok);
  const aiFailing = aiModels.filter((m) => m.ok === false);

  return (
    <>
      <div className="ambient" aria-hidden="true">
        <span className="orb orb-1" />
        <span className="orb orb-2" />
        <span className="orb orb-3" />
      </div>

      <div className="app">
      <div className={`masthead${condensed ? " condensed" : ""}`}>
        <div>
          <h1>Autonomous CI/CD Healing Agent</h1>
          <p className="tagline">
            Clones a repository, finds real defects, repairs them, validates the
            result through a CI/CD gate loop, and pushes a healed branch — while
            telling apart “your code broke the build” from “the platform is
            degraded”.
          </p>
        </div>
        <div className="masthead-actions">
          {health && (
            <span className={`pill ${health.status === "ok" ? "good" : "warn"}`}>
              <span className="dot" />
              API {health.status} · v{health.version}
            </span>
          )}
          {aiModels.length === 0 ? (
            <span className="pill muted">
              <span className="dot" />
              rule-based only
            </span>
          ) : (
            aiModels.map((m) => (
              <span
                key={m.provider}
                className={`pill ${m.ok ? "good" : "warn"}`}
                title={`${m.model}: ${m.detail}`}
              >
                <span className="dot" />
                {m.provider === "gemini" ? "Gemini" : "Claude"}{" "}
                {m.ok ? "ready" : "failing"}
              </span>
            ))
          )}
          <MemoryPill health={health} />
          <button
            className="icon-button"
            onClick={() =>
              setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark")
            }
            title="Toggle colour theme"
          >
            theme: {theme}
          </button>
        </div>
      </div>

      {!health && (
        <div className="banner warn">
          Cannot reach the backend. Start it with{" "}
          <span className="mono">uvicorn healing_agent.app:app --port 8000</span>{" "}
          (from the <span className="mono">backend/</span> directory), or set{" "}
          <span className="mono">VITE_API_BASE_URL</span>.
        </div>
      )}

      {aiFailing.length > 0 && (
        <div className="banner warn">
          {aiWorking
            ? "A configured AI model is failing; the agent falls back to the other one. "
            : "Every configured AI model is failing, so only rule-based repairs will run. "}
          {aiFailing.map((m) => (
            <div key={m.provider}>
              <strong>{m.provider === "gemini" ? "Gemini" : "Claude"}</strong>{" "}
              <span className="mono">({m.model})</span>: {m.detail}
            </div>
          ))}
        </div>
      )}

      {health && !aiEnabled && (
        <div className="banner info">
          No AI key configured (<span className="mono">ANTHROPIC_API_KEY</span> or{" "}
          <span className="mono">GEMINI_API_KEY</span>) — the agent still detects
          problems and applies deterministic repairs, but the AI repair tier is
          disabled.
        </div>
      )}

      {error && <div className="banner error">{error}</div>}

      <div className="layout">
        <div>
          <AnalyzeForm onStart={handleStart} running={running} />
          {job && <Progress job={job} elapsed={elapsed} />}
          {job && <ActivityLog logs={job.logs} />}
          <RecentRuns currentId={job?.job_id} onOpen={openJob} />
        </div>

        <div>
          {!job ? (
            <div className="card">
              <header><h2>How it works</h2></header>
              <ol style={{ fontSize: "0.84rem", color: "var(--text-secondary)", paddingLeft: 18, margin: 0, lineHeight: 1.8 }}>
                <li><strong>Detect</strong> — clones the repo and scans for syntax errors, bad indentation, unresolved imports, type and lint defects, and malformed workflow files.</li>
                <li><strong>Remember</strong> — recalls similar past incidents from Hindsight memory before diagnosing, and retains every run afterwards so the next similar failure is recognised.</li>
                <li><strong>Diagnose</strong> — reads Actions telemetry and the provider status page to classify failures as code-level or platform-level, with the evidence shown.</li>
                <li><strong>Heal</strong> — applies deterministic repairs first, then model-generated ones; every model patch must parse and reduce the problem count or it is rolled back.</li>
                <li><strong>Validate</strong> — runs syntax, imports, lint, compile, and test gates, looping until they pass or no further repair is possible.</li>
                <li><strong>Communicate</strong> — pushes a branch and writes a post-incident report with the root cause and what was changed.</li>
              </ol>
            </div>
          ) : (
            <>
              <StatTiles job={job} elapsed={elapsed} />
              <ResultPanel job={job} />
              <ScoreCard job={job} />
              <Diagnosis job={job} />
              <Memory job={job} />
              <Validation job={job} />
              <Findings job={job} />
              <Fixes job={job} />
              <PipelineHealth job={job} />
              <RepoHealth job={job} />
            </>
          )}
        </div>
      </div>

      <div className="footer">
        <span>Autonomous CI/CD Healing Agent</span>
        <span>
          {health?.config?.repo_backend
            ? `repo backend: ${health.config.repo_backend} · model: ${health.config.model}`
            : ""}
        </span>
      </div>
      </div>
    </>
  );
}
