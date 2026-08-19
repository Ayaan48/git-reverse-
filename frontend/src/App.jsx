import React, { useCallback, useEffect, useRef, useState } from "react";
import AnalyzeForm from "./components/AnalyzeForm.jsx";
import Progress from "./components/Progress.jsx";
import StatTiles from "./components/StatTiles.jsx";
import Findings from "./components/Findings.jsx";
import Fixes from "./components/Fixes.jsx";
import Validation from "./components/Validation.jsx";
import Diagnosis from "./components/Diagnosis.jsx";
import ScoreCard from "./components/ScoreCard.jsx";
import ActivityLog from "./components/ActivityLog.jsx";
import ResultPanel from "./components/ResultPanel.jsx";
import { PipelineHealth, RepoHealth } from "./components/HealthPanels.jsx";
import { getHealth, startAnalysis, subscribeToJob } from "./api.js";

const TERMINAL = ["succeeded", "partial", "failed", "cancelled"];

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

  const aiEnabled = health?.checks?.ai_repair_tier;

  return (
    <div className="app">
      <div className="masthead">
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
          <span className={`pill ${aiEnabled ? "good" : "muted"}`}>
            <span className="dot" />
            {aiEnabled ? "AI repairs on" : "rule-based only"}
          </span>
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

      {health && !aiEnabled && (
        <div className="banner info">
          No <span className="mono">ANTHROPIC_API_KEY</span> configured — the agent
          still detects problems and applies deterministic repairs, but the
          AI repair tier is disabled.
        </div>
      )}

      {error && <div className="banner error">{error}</div>}

      <div className="layout">
        <div>
          <AnalyzeForm onStart={handleStart} running={running} />
          {job && <Progress job={job} elapsed={elapsed} />}
          {job && <ActivityLog logs={job.logs} />}
        </div>

        <div>
          {!job ? (
            <div className="card">
              <header><h2>How it works</h2></header>
              <ol style={{ fontSize: "0.84rem", color: "var(--text-secondary)", paddingLeft: 18, margin: 0, lineHeight: 1.8 }}>
                <li><strong>Detect</strong> — clones the repo and scans for syntax errors, bad indentation, unresolved imports, type and lint defects, and malformed workflow files.</li>
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
  );
}
