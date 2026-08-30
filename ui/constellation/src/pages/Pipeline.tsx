import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { approveRun, streamRun } from "../api";
import QodoPanel from "../components/QodoPanel";
import Topbar from "../components/Topbar";
import type { ApprovalRequest, StageRow } from "../types";

export default function Pipeline() {
  const { runId } = useParams();
  const nav = useNavigate();
  const [rows, setRows] = useState<(StageRow & { ticker: string })[]>([]);
  const [total, setTotal] = useState(1);
  const [task, setTask] = useState("");
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [err, setErr] = useState("");
  const finished = useRef(false);

  useEffect(() => {
    if (!runId) return;

    const stop = streamRun(runId, {
      onStart: (d) => {
        setTotal(Math.max(d.total, 1));
        setTask(d.slug);
      },
      onStage: (d) => {
        setRows((prev) => {
          const next = [...prev];
          const at = next.findIndex((r) => r.key === d.key);
          if (at >= 0) next[at] = { ...next[at], ...d };
          else next.push({ ...d, ticker: "" });
          return next;
        });
      },
      onLog: (d) =>
        setRows((prev) =>
          prev.map((r) => (r.key === d.key ? { ...r, ticker: d.line } : r)),
        ),
      onApproval: (d) => setApproval(d),
      onResolved: () => {
        finished.current = true;
        setTimeout(() => nav(`/result/${runId}`), 600);
      },
      onDone: (d) => {
        if (!d.result.green) {
          finished.current = true;
          setTimeout(() => nav(`/result/${runId}`), 900);
        }
      },
      onError: () => {
        if (!finished.current)
          setErr("stream interrupted — retrying may be needed");
      },
    });
    return stop;
  }, [runId, nav]);

  async function decide(allow: boolean) {
    if (!runId || deciding) return;
    setDeciding(true);
    try {
      await approveRun(runId, allow);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "approval failed");
      setDeciding(false);
    }
  }

  const settled = rows.filter((r) => r.status !== "active").length;
  const pct = Math.round((settled / total) * 100);

  return (
    <div className="shell">
      <Topbar meta="searching" />
      <div className="main">
        <div className="run-head">
          <h2 className="run-goal">
            Searching for a green patch
            <span className="caret" />
          </h2>
          <div className="run-sub">
            run {runId} · {task || "…"} · {settled}/{total} attempts graded
          </div>
        </div>

        <div className="progress">
          <i style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>

        {rows.map((r) => (
          <div key={r.key} className={`stage ${r.status}`} data-testid="stage">
            <div className="ind" />
            <div>
              <div className="label">{r.label}</div>
              <div className="detail">
                {r.detail}
                {r.score !== undefined && ` · score ${r.score?.toFixed(2)}`}
                {r.outcome && ` · ${r.outcome}`}
              </div>
              {(r.findings?.length ?? 0) > 0 && (
                <div className="findings">
                  {r.findings!.map((f) => (
                    <span className="finding bad" key={f}>
                      {f}
                    </span>
                  ))}
                </div>
              )}
              {r.status === "pruned" && r.reason && (
                <div className="prune-reason">✂ {r.reason}</div>
              )}
              {r.status === "active" && r.ticker && (
                <div className="ticker" key={r.ticker}>
                  › {r.ticker}
                </div>
              )}
            </div>
            <div className="layer">{r.layer}</div>
          </div>
        ))}

        {approval && (
          <div className="card approval" data-testid="approval">
            <div className="k">approval gate — nothing ships without you</div>
            <div className="approval-summary">{approval.summary}</div>
            <div className="row">
              <span>
                {approval.stats.nodes_explored} nodes · score{" "}
                {approval.stats.score?.toFixed(2)} · $
                {approval.stats.cost_usd?.toFixed(2)}
              </span>
            </div>
            {approval.diff_preview && (
              <pre className="diff">{approval.diff_preview}</pre>
            )}
            <div className="actions">
              <button
                className="btn"
                disabled={deciding}
                onClick={() => decide(true)}
              >
                approve
              </button>{" "}
              <button
                className="btn deny"
                disabled={deciding}
                onClick={() => decide(false)}
              >
                deny
              </button>
            </div>
          </div>
        )}

        <QodoPanel />

        {err && <div className="err">{err}</div>}
      </div>
    </div>
  );
}
