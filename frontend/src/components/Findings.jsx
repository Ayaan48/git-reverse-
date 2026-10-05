import React, { useMemo, useState } from "react";

const ORDER = ["critical", "high", "medium", "low"];
const WEIGHT = { critical: 10, high: 6, medium: 3, low: 1 };
const TERMINAL = ["succeeded", "partial", "failed", "cancelled"];
const keyOf = (problem) => `${problem.file}:${problem.line}:${problem.code}`;

/**
 * Severity is shown as separately labelled rows, never as adjacent colour-only
 * segments of one stacked bar. Two of the status hues sit below the
 * normal-vision separation floor, so each row carries its name and count in
 * text and colour is only ever reinforcement.
 */
export function SeverityBreakdown({ counts }) {
  const total = ORDER.reduce((sum, key) => sum + (counts?.[key] ?? 0), 0);
  if (!total) return <p className="empty">No problems detected.</p>;

  return (
    <div className="severity-rows">
      {ORDER.map((name) => {
        const count = counts?.[name] ?? 0;
        const pct = total ? (count / total) * 100 : 0;
        return (
          <div className="severity-row" key={name}>
            <span className="severity-name">{name}</span>
            <div className="severity-track">
              {count > 0 && (
                <div className={`severity-bar sev-${name}`} style={{ width: `${pct}%` }} />
              )}
            </div>
            <span className="severity-count">{count}</span>
          </div>
        );
      })}
    </div>
  );
}

export function SeverityBadge({ severity }) {
  return (
    <span className={`badge ${severity}`}>
      <span className="swatch" aria-hidden="true" />
      {severity}
    </span>
  );
}

/* ------------------------------------------------------------ code view */

/**
 * Code text for one row. On changed and problem lines, tabs and trailing
 * whitespace are made visible: many fixes (mixed indentation, trailing
 * spaces) would otherwise look like identical red and green lines.
 */
function CodeText({ text, reveal }) {
  if (!text) return " ";
  if (!reveal) return text;
  const trailAt = text.search(/[ \t]+$/);
  const end = trailAt === -1 ? text.length : trailAt;
  const out = [];
  let plain = "";
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const trailing = i >= end;
    if (ch === "\t" || (ch === " " && trailing)) {
      if (plain) {
        out.push(plain);
        plain = "";
      }
      out.push(
        <span key={i} className={`ws${ch === "\t" ? " tab" : ""}${trailing ? " trail" : ""}`}>
          {ch === "\t" ? "→" : "·"}
        </span>,
      );
    } else {
      plain += ch;
    }
  }
  if (plain) out.push(plain);
  return out;
}

const SIGN = { del: "−", add: "+", problem: "!", context: " " };
const SPOKEN = { del: "removed", add: "added", problem: "problem line" };

function LineRow({ type, old, now, text }) {
  return (
    <tr className={`line ${type}`}>
      <td className="num">{old ?? ""}</td>
      <td className="num">{now ?? ""}</td>
      <td className="sign">
        <span aria-hidden="true">{SIGN[type]}</span>
        {SPOKEN[type] && <span className="sr-only">{SPOKEN[type]}</span>}
      </td>
      <td className="code">
        <CodeText text={text} reveal={type !== "context"} />
      </td>
    </tr>
  );
}

const STATE = { resolved: ["fixed", "ok"], remaining: ["still there", "bad"] };

function AnnotationRow({ problems, status }) {
  return (
    <tr className="annot">
      <td colSpan={4}>
        <div className="annot-box">
          {problems.map((problem, index) => {
            const state = STATE[status[keyOf(problem)]];
            return (
              <div className="annot-item" key={`${problem.code}-${index}`}>
                <SeverityBadge severity={problem.severity} />
                <span className="mono">{problem.code}</span>
                <span className="annot-msg">{problem.message}</span>
                {state && <span className={`state ${state[1]}`}>{state[0]}</span>}
              </div>
            );
          })}
        </div>
      </td>
    </tr>
  );
}

const SubRow = ({ children }) => (
  <tr className="sub">
    <td colSpan={4}>{children}</td>
  </tr>
);

function FileReview({ entry, status, open }) {
  const { file, problems, excerpt, diff } = entry;

  const byLine = new Map();
  for (const problem of problems) {
    const list = byLine.get(problem.line) ?? [];
    list.push(problem);
    byLine.set(problem.line, list);
  }

  const rows = [];
  const placed = new Set();
  const annotate = (line, key) => {
    const list = byLine.get(line);
    if (list && !placed.has(line)) {
      placed.add(line);
      rows.push(<AnnotationRow key={`${key}-note`} problems={list} status={status} />);
    }
  };

  // 1. What the agent changed: GitHub-style hunks, problems pinned to the
  //    original lines they were found on.
  if (diff) {
    diff.hunks.forEach((hunk, h) => {
      rows.push(
        <tr className="hunk" key={`h${h}`}>
          <td colSpan={4}>
            @@ {"−"}{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@
          </td>
        </tr>,
      );
      hunk.rows.forEach((row, r) => {
        const key = `h${h}r${r}`;
        if (row.type === "note") {
          rows.push(
            <tr className="line note" key={key}>
              <td className="num" />
              <td className="num" />
              <td className="sign" />
              <td className="code">{"\\ "}{row.text}</td>
            </tr>,
          );
          return;
        }
        rows.push(<LineRow key={key} type={row.type} old={row.old} now={row.new} text={row.text} />);
        if (row.old != null) annotate(row.old, key);
      });
    });
    if (diff.truncated) {
      rows.push(<SubRow key="truncated">The rest of this file's changes are not shown.</SubRow>);
    }
  }

  // 2. Problems on lines the diff didn't touch, shown from the original code.
  const pending = new Set([...byLine.keys()].filter((line) => !placed.has(line)));
  if (pending.size && excerpt) {
    if (diff) rows.push(<SubRow key="unchanged">Problems on lines the agent did not change</SubRow>);
    let first = true;
    excerpt.ranges.forEach((range, g) => {
      const numbers = range.lines.map((_, i) => range.start + i);
      if (diff && !numbers.some((n) => pending.has(n))) return;
      if (!first) {
        rows.push(
          <tr className="gap" key={`gap${g}`}>
            <td colSpan={4}>{"⋯"}</td>
          </tr>,
        );
      }
      first = false;
      range.lines.forEach((text, i) => {
        const n = range.start + i;
        const isProblem = pending.has(n) && !placed.has(n);
        rows.push(
          <LineRow
            key={`e${g}-${i}`}
            type={isProblem ? "problem" : "context"}
            old={n}
            now={diff ? null : n}
            text={text}
          />,
        );
        if (isProblem) annotate(n, `e${g}-${i}`);
      });
    });
  }

  // 3. Anything left (no excerpt available, or a whole-file problem).
  const leftovers = [...byLine.keys()].filter((line) => !placed.has(line));
  if (leftovers.length) {
    rows.push(<SubRow key="other">Other problems in this file</SubRow>);
    leftovers.forEach((line) => annotate(line, `left${line}`));
  }

  const fixed = problems.filter((p) => status[keyOf(p)] === "resolved").length;
  const judged = problems.some((p) => status[keyOf(p)]);

  return (
    <details className="file-review" open={open}>
      <summary>
        <span className="file-name mono">{file}</span>
        <span className="file-meta">
          {problems.length > 0 && (
            <span>
              {problems.length} problem{problems.length === 1 ? "" : "s"}
            </span>
          )}
          {judged && problems.length > 0 && (
            <span className={fixed === problems.length ? "all-fixed" : ""}>
              {fixed}/{problems.length} fixed
            </span>
          )}
          {diff ? (
            <span>
              <span className="diff-count-add">+{diff.additions}</span>{" "}
              <span className="diff-count-del">{"−"}{diff.deletions}</span>
            </span>
          ) : (
            judged && <span>not changed</span>
          )}
        </span>
      </summary>
      {diff && diff.original_known === false && (
        <p className="code-note">The original file wasn't captured, so it is shown as all new.</p>
      )}
      {rows.length ? (
        <div className="code-scroll">
          <table className="code-table">
            <tbody>{rows}</tbody>
          </table>
        </div>
      ) : (
        <p className="code-note">No code to show for this file.</p>
      )}
    </details>
  );
}

function CodeReview({ job }) {
  const [limit, setLimit] = useState(6);
  const problems = job?.problems ?? [];
  const status = job?.problem_status ?? {};
  const diffs = job?.diffs ?? [];
  const excerpts = job?.excerpts ?? [];

  const entries = useMemo(() => {
    const map = new Map();
    const entry = (file) => {
      if (!map.has(file)) {
        map.set(file, { file, problems: [], weight: 0, excerpt: null, diff: null });
      }
      return map.get(file);
    };
    for (const problem of problems) {
      const item = entry(problem.file);
      item.problems.push(problem);
      item.weight += WEIGHT[problem.severity] ?? 1;
    }
    for (const diff of diffs) entry(diff.file).diff = diff;
    for (const excerpt of excerpts) if (map.has(excerpt.file)) map.get(excerpt.file).excerpt = excerpt;
    return [...map.values()].sort((a, b) => b.weight - a.weight || a.file.localeCompare(b.file));
  }, [problems, diffs, excerpts]);

  const finished = TERMINAL.includes(job?.status);
  const waiting = !diffs.length && !finished;

  return (
    <>
      <div className="code-legend">
        <span className="key key-del">
          <i aria-hidden="true" /> Problem line or removed code
        </span>
        <span className="key key-add">
          <i aria-hidden="true" /> The agent's corrected code
        </span>
        {waiting && <span>Corrected code appears here once the agent finishes healing.</span>}
        {finished && !diffs.length && <span>The agent didn't change any files in this run.</span>}
      </div>
      <div className="file-reviews">
        {entries.slice(0, limit).map((item, index) => (
          <FileReview key={item.file} entry={item} status={status} open={index < 3} />
        ))}
      </div>
      {entries.length > limit && (
        <button className="icon-button" style={{ marginTop: 10 }} onClick={() => setLimit((v) => v + 6)}>
          Show more files ({entries.length - limit} hidden)
        </button>
      )}
    </>
  );
}

/**
 * The diff rows that show one problem's line: its whole change block (or the
 * unchanged line itself) with up to two lines of context either side.
 */
function diffSlice(diff, line) {
  const changed = (row) => row.type === "del" || row.type === "add" || row.type === "note";
  for (const hunk of diff?.hunks ?? []) {
    const rows = hunk.rows;
    const at = rows.findIndex((row) => row.old === line);
    if (at === -1) continue;
    let start = at;
    let end = at;
    if (changed(rows[at])) {
      while (start > 0 && changed(rows[start - 1])) start -= 1;
      while (end < rows.length - 1 && changed(rows[end + 1])) end += 1;
    }
    const slice = rows.slice(Math.max(0, start - 2), Math.min(rows.length, end + 3));
    return {
      changed: changed(rows[at]),
      rows: slice.map((row) =>
        row.old === line && row.type === "context" ? { ...row, type: "problem" } : row,
      ),
    };
  }
  return null;
}

/** The original lines around one problem, from the excerpts taken before healing. */
function excerptSlice(excerpts, file, line) {
  const excerpt = (excerpts ?? []).find((item) => item.file === file);
  for (const range of excerpt?.ranges ?? []) {
    const last = range.start + range.lines.length - 1;
    if (line < range.start || line > last) continue;
    const rows = [];
    for (let n = Math.max(range.start, line - 2); n <= Math.min(last, line + 2); n += 1) {
      rows.push({ type: n === line ? "problem" : "context", old: n, new: n, text: range.lines[n - range.start] });
    }
    return rows;
  }
  return null;
}

/** The code for one problem, shown when its row is expanded. */
function ProblemCode({ problem, job }) {
  const state = STATE[job?.problem_status?.[keyOf(problem)]];
  const diff = (job?.diffs ?? []).find((item) => item.file === problem.file);
  const slice = diffSlice(diff, problem.line);
  const rows = slice ? slice.rows : excerptSlice(job?.excerpts, problem.file, problem.line);
  const finished = TERMINAL.includes(job?.status);

  let lead;
  if (slice?.changed) lead = "Changed by the agent: removed lines in red, corrected lines in green.";
  else if (!rows) lead = "The code for this line isn't available.";
  else if (finished) lead = "The agent did not change this line.";
  else lead = "The agent's correction, if any, appears here once healing finishes.";

  return (
    <div className="problem-code">
      <div className="problem-code-head">
        <span className="mono">
          {problem.file}:{problem.line}
        </span>
        {state && <span className={`state ${state[1]}`}>{state[0]}</span>}
        <span>{lead}</span>
      </div>
      {rows && (
        <div className="code-scroll">
          <table className="code-table">
            <tbody>
              {rows.map((row, index) =>
                row.type === "note" ? (
                  <tr className="line note" key={index}>
                    <td className="num" />
                    <td className="num" />
                    <td className="sign" />
                    <td className="code">{"\\ "}{row.text}</td>
                  </tr>
                ) : (
                  <LineRow key={index} type={row.type} old={row.old} now={row.new} text={row.text} />
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ProblemTable({ job }) {
  const problems = job?.problems ?? [];
  const status = job?.problem_status ?? {};
  const [limit, setLimit] = useState(25);
  const [open, setOpen] = useState(() => new Set());

  const visible = problems.slice(0, limit);
  const rowKey = (problem, index) => `${keyOf(problem)}#${index}`;
  const toggle = (key) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const allOpen = visible.length > 0 && visible.every((p, i) => open.has(rowKey(p, i)));

  return (
    <>
      <div className="table-tools">
        <span className="hint">Press {"▾"} on a problem to see its code.</span>
        <button
          type="button"
          className="icon-button"
          onClick={() => setOpen(allOpen ? new Set() : new Set(visible.map(rowKey)))}
        >
          {allOpen ? "Collapse all" : "Expand all"}
        </button>
      </div>
      <div className="table-scroll">
        <table className="problem-table">
          <thead>
            <tr>
              <th>
                <span className="sr-only">Show code</span>
              </th>
              <th>Severity</th>
              <th>Location</th>
              <th>Code</th>
              <th>Problem</th>
              <th>Result</th>
              <th>Detector</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((problem, index) => {
              const key = rowKey(problem, index);
              const isOpen = open.has(key);
              const detailId = `problem-code-${index}`;
              const state = STATE[status[keyOf(problem)]];
              return (
                <React.Fragment key={key}>
                  <tr className={isOpen ? "row-open" : undefined}>
                    <td className="toggle-cell">
                      <button
                        type="button"
                        className="row-toggle"
                        aria-expanded={isOpen}
                        aria-controls={isOpen ? detailId : undefined}
                        onClick={() => toggle(key)}
                      >
                        <span className="chev" aria-hidden="true">
                          {"▾"}
                        </span>
                        <span className="sr-only">
                          {isOpen ? "Hide" : "Show"} the code for {problem.code} at {problem.file} line{" "}
                          {problem.line}
                        </span>
                      </button>
                    </td>
                    <td><SeverityBadge severity={problem.severity} /></td>
                    <td className="mono">{problem.file}:{problem.line}</td>
                    <td className="mono nowrap">{problem.code}</td>
                    <td className="wrap">{problem.message}</td>
                    <td className="nowrap">
                      {state ? (
                        <span className={`state ${state[1]}`}>{state[0]}</span>
                      ) : (
                        <span className="pending">{"–"}</span>
                      )}
                    </td>
                    <td className="mono">{problem.detector}</td>
                  </tr>
                  {isOpen && (
                    <tr className="problem-detail" id={detailId}>
                      <td colSpan={7}>
                        <ProblemCode problem={problem} job={job} />
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
        {problems.length > limit && (
          <button
            className="icon-button"
            style={{ marginTop: 10 }}
            onClick={() => setLimit((value) => value + 50)}
          >
            Show more ({problems.length - limit} hidden)
          </button>
        )}
      </div>
    </>
  );
}

export default function Findings({ job }) {
  const [view, setView] = useState("list");
  const problems = job?.problems ?? [];

  return (
    <div className="card">
      <header>
        <h2>Problems detected ({problems.length})</h2>
        <span className="hint">
          {Object.entries(job?.problems_by_kind ?? {})
            .map(([kind, count]) => `${kind} ${count}`)
            .join(" · ")}
        </span>
      </header>

      <SeverityBreakdown counts={job?.problems_by_severity} />

      {problems.length > 0 && (
        <>
          <div className="view-switch" role="group" aria-label="Show problems as">
            <button type="button" aria-pressed={view === "list"} onClick={() => setView("list")}>
              List
            </button>
            <button type="button" aria-pressed={view === "code"} onClick={() => setView("code")}>
              By file
            </button>
          </div>
          {view === "code" ? <CodeReview job={job} /> : <ProblemTable job={job} />}
        </>
      )}
    </div>
  );
}
