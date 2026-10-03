import React, { useEffect, useState } from "react";
import { listJobs } from "../api.js";

const TONE = {
  succeeded: "good",
  partial: "warn",
  failed: "bad",
  cancelled: "muted",
  running: "warn",
  queued: "muted",
};

/**
 * Runs the server knows about, including ones started automatically by a
 * GitHub Actions trigger, so they can be opened and watched live here.
 */
export default function RecentRuns({ currentId, onOpen }) {
  const [jobs, setJobs] = useState([]);

  useEffect(() => {
    let alive = true;
    const load = () =>
      listJobs()
        .then((data) => alive && setJobs(data?.jobs ?? []))
        .catch(() => {});
    load();
    const timer = setInterval(load, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  if (!jobs.length) return null;

  return (
    <div className="card">
      <header>
        <h2>Recent runs</h2>
        <span className="hint">including runs started automatically when CI failed</span>
      </header>
      <ul className="runs">
        {jobs.slice(0, 8).map((job) => (
          <li key={job.job_id}>
            <button
              type="button"
              className={`run-item ${job.job_id === currentId ? "current" : ""}`}
              onClick={() => onOpen(job.job_id)}
              aria-current={job.job_id === currentId ? "true" : undefined}
            >
              <span className="run-top">
                <span className="mono run-repo">{job.repo}</span>
                <span className={`pill ${TONE[job.status] ?? "muted"}`}>
                  <span className="dot" />
                  {job.status}
                </span>
              </span>
              <span className="run-meta">
                {job.trigger ? job.trigger : `branch ${job.branch_name}`}
                {job.ci ? ` · real CI ${job.ci}` : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
