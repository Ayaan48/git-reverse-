import React from "react";

const GRADE_COLOR = (grade) => {
  if (!grade) return "var(--text-muted)";
  if (grade.startsWith("A")) return "var(--status-good)";
  if (grade.startsWith("B")) return "var(--accent)";
  if (grade.startsWith("C")) return "var(--status-warning)";
  if (grade.startsWith("D")) return "var(--status-serious)";
  return "var(--status-critical)";
};

export default function ScoreCard({ job }) {
  const score = job?.score;
  if (!score || score.total == null) return null;

  const color = GRADE_COLOR(score.grade);

  return (
    <div className="card">
      <header>
        <h2>Run score</h2>
        <span className="hint">weighted by repair impact, gates, speed, and criticals cleared</span>
      </header>
      <div className="score-wrap">
        <div className="score-hero">
          <span className="score-number" style={{ color }}>{score.total}</span>
          <span className="score-max">/ 100</span>
          <span className="score-grade" style={{ color, borderColor: color }}>
            {score.grade}
          </span>
        </div>
        <div className="score-breakdown">
          {score.breakdown.map((item) => (
            <div className="score-item" key={item.key}>
              <span>{item.label}</span>
              <span className="mono">{item.points}/{item.max}</span>
              <div className="bar-track">
                <div
                  className="bar-fill"
                  style={{ width: `${(item.points / item.max) * 100}%` }}
                />
              </div>
              <span className="detail">{item.detail}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
