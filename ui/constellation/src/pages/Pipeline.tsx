import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { streamRun } from "../api";
import Topbar from "../components/Topbar";

interface Row {
  index: number;
  key: string;
  label: string;
  detail: string;
  layer: string;
  status: "active" | "done";
  ticker: string;
}

export default function Pipeline() {
  const { runId } = useParams();
  const nav = useNavigate();
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(9);
  const [err, setErr] = useState("");
  const done = useRef(false);

  useEffect(() => {
    if (!runId) return;

    const stop = streamRun(runId, {
      onStart: (d) => setTotal(d.total),
      onStage: (d) => {
        setRows((prev) => {
          const next = [...prev];
          const at = next.findIndex((r) => r.index === d.index);
          if (d.status === "active") {
            const row: Row = {
              index: d.index,
              key: d.key,
              label: d.label,
              detail: d.detail,
              layer: d.layer,
              status: "active",
              ticker: "",
            };
            if (at >= 0) next[at] = row;
            else next.push(row);
          } else if (at >= 0) {
            next[at] = { ...next[at], status: "done" };
          }
          return next;
        });
      },
      onLog: (d) =>
        setRows((prev) =>
          prev.map((r) => (r.key === d.key ? { ...r, ticker: d.line } : r)),
        ),
      onDone: (d) => {
        done.current = true;
        setTimeout(() => nav(`/result/${d.result.run_id}`), 600);
      },
      onError: () => {
        if (!done.current)
          setErr("stream interrupted — retrying may be needed");
      },
    });
    return stop;
  }, [runId, nav]);

  const doneCount = rows.filter((r) => r.status === "done").length;
  const pct = Math.round((doneCount / total) * 100);

  return (
    <div className="shell">
      <Topbar meta="building" />
      <div className="main">
        <div className="run-head">
          <h2 className="run-goal">
            Building
            <span className="caret" />
          </h2>
          <div className="run-sub">
            run {runId} · {doneCount}/{total} stages
          </div>
        </div>

        <div className="progress">
          <i style={{ width: `${pct}%` }} />
        </div>

        {rows.map((r) => (
          <div key={r.index} className={`stage ${r.status}`}>
            <div className="ind" />
            <div>
              <div className="label">{r.label}</div>
              <div className="detail">{r.detail}</div>
              {r.status === "active" && r.ticker && (
                <div className="ticker" key={r.ticker}>
                  › {r.ticker}
                </div>
              )}
            </div>
            <div className="layer">{r.layer}</div>
          </div>
        ))}

        {err && <div className="err">{err}</div>}
      </div>
    </div>
  );
}
