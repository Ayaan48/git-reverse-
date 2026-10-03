import React, { useState } from "react";

const CI_LABEL = {
  passed: ["good", "Real CI passed on the fix"],
  failed: ["bad", "Real CI failed on the fix"],
  no_ci: ["muted", "No CI run started for the fix"],
  timed_out: ["warn", "Real CI still running at the deadline"],
  error: ["warn", "Could not read the CI result"],
};

export default function ResultPanel({ job }) {
  const [showReport, setShowReport] = useState(false);
  if (!job) return null;

  const finished = ["succeeded", "partial", "failed"].includes(job.status);
  if (!finished && !job.branch_url) return null;

  return (
    <>
      {job.branch_url && (
        <div className="card">
          <div className="branch-cta">
            <div>
              <strong>Branch pushed:</strong>{" "}
              <span className="mono">{job.branch_name}</span>
              {job.commit_sha && (
                <span className="mono" style={{ color: "var(--text-muted)" }}>
                  {" "}· {job.commit_sha.slice(0, 8)}
                </span>
              )}
            </div>
            <div className="branch-links">
              {job.pull_request_url ? (
                <a className="primary" href={job.pull_request_url} target="_blank" rel="noreferrer">
                  View pull request
                </a>
              ) : (
                job.compare_url && (
                  <a className="primary" href={job.compare_url} target="_blank" rel="noreferrer">
                    Open pull request
                  </a>
                )
              )}
              <a href={job.branch_url} target="_blank" rel="noreferrer">
                View branch
              </a>
            </div>
          </div>
        </div>
      )}

      {job.ci_verification?.status && (
        <div className="card">
          <div className="ci-result">
            <span className={`pill ${(CI_LABEL[job.ci_verification.status] ?? ["muted"])[0]}`}>
              <span className="dot" />
              {(CI_LABEL[job.ci_verification.status] ?? [null, job.ci_verification.status])[1]}
            </span>
            <span className="ci-detail">{job.ci_verification.detail}</span>
            {(job.ci_verification.runs ?? []).map((run) =>
              run.url ? (
                <a key={run.url} href={run.url} target="_blank" rel="noreferrer" className="mono">
                  {run.name}: {run.conclusion ?? run.status}
                </a>
              ) : null,
            )}
          </div>
        </div>
      )}

      {job.error && <div className="banner error">{job.error}</div>}

      {job.incident_report && (
        <div className="card">
          <header>
            <h2>Post-incident report</h2>
            <button className="icon-button" onClick={() => setShowReport((value) => !value)}>
              {showReport ? "Hide" : "Show"}
            </button>
          </header>
          {showReport && <pre className="report">{job.incident_report}</pre>}
        </div>
      )}
    </>
  );
}
