import React, { useEffect, useRef } from "react";

export default function ActivityLog({ logs }) {
  const endRef = useRef(null);
  const entries = logs ?? [];

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [entries.length]);

  return (
    <div className="card">
      <header>
        <h2>Activity log</h2>
        <span className="hint">{entries.length} entries</span>
      </header>
      {entries.length === 0 ? (
        <p className="empty">Waiting for the agent to start…</p>
      ) : (
        <div className="log">
          {entries.map((entry, index) => (
            <div className={`log-line ${entry.level}`} key={index}>
              <span className="log-time">
                {new Date(entry.ts * 1000).toLocaleTimeString([], {
                  hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
                })}
              </span>
              <span className="log-msg">{entry.message}</span>
            </div>
          ))}
          <div ref={endRef} />
        </div>
      )}
    </div>
  );
}
