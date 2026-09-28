import React from "react";

/** The masthead pill: is persistent memory configured and reachable? */
export function MemoryPill({ health }) {
  const memory = health?.checks?.memory;
  if (!memory) return null;
  const [tone, label] = !memory.enabled
    ? ["muted", "memory off"]
    : memory.connected
      ? ["good", "memory connected"]
      : ["warn", "memory unreachable"];
  return (
    <span className={`pill ${tone}`} title={memory.detail || memory.bank_id}>
      <span className="dot" />
      {label}
    </span>
  );
}

/** What memory contributed to this run: recalled incidents and their influence. */
export default function Memory({ job }) {
  const memory = job?.memory;
  if (!memory || !Object.keys(memory).length) return null;

  const memories = memory.memories ?? [];
  const influenced = memory.influenced_incidents ?? [];

  return (
    <div className="card">
      <header>
        <h2>Incident memory</h2>
        <span className="hint">
          {memory.enabled
            ? `Hindsight bank: ${memory.bank_id}`
            : "past incidents recalled before diagnosis"}
        </span>
      </header>

      {!memory.enabled ? (
        <p className="empty">
          Memory is off ({memory.disabled_reason}). The agent diagnosed this
          run from live evidence only. Set <span className="mono">HINDSIGHT_API_KEY</span>{" "}
          so it can learn from each incident.
        </p>
      ) : memory.error ? (
        <p className="empty">Recall failed ({memory.error}). Diagnosed without memory.</p>
      ) : (
        <>
          <div className="verdict">
            <span className="pill muted">
              <span className="dot" />
              {memory.incidents_found} similar past incident
              {memory.incidents_found === 1 ? "" : "s"}
            </span>
            <span className={`pill ${influenced.length ? "good" : "muted"}`}>
              <span className="dot" />
              {influenced.length} influenced diagnosis
            </span>
            {memory.retained !== null && memory.retained !== undefined && (
              <span className={`pill ${memory.retained ? "good" : "warn"}`}>
                <span className="dot" />
                {memory.retained ? "this run retained" : "retain failed"}
              </span>
            )}
          </div>

          {memories.length === 0 ? (
            <p className="empty">
              No similar incidents yet. This one will be remembered for next time.
            </p>
          ) : (
            <ul className="fix-list">
              {memories.slice(0, 8).map((item) => (
                <li
                  className={`fix-item memory ${item.influenced ? "influenced" : ""}`}
                  key={item.id}
                >
                  <div className="fix-head">
                    <span className="tier-tag">{item.failure_class || "unclassified"}</span>
                    {item.repo && <span className="fix-file">{item.repo}</span>}
                    {item.influenced && <span className="tier-tag">influenced diagnosis</span>}
                  </div>
                  <div className="fix-desc">{item.text}</div>
                </li>
              ))}
            </ul>
          )}

          {memory.risk_notes && (
            <>
              <h3 style={{ marginTop: 16, marginBottom: 8 }}>Pre-merge risk notes</h3>
              <p className="fix-desc" style={{ whiteSpace: "pre-wrap" }}>
                {memory.risk_notes}
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}
