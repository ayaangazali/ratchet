import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getResult } from "../api";
import QodoPanel from "../components/QodoPanel";
import Topbar from "../components/Topbar";
import type { RunResult } from "../types";

export default function Result() {
  const { runId } = useParams();
  const [r, setR] = useState<RunResult | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!runId) return;
    getResult(runId)
      .then(setR)
      .catch((e) => setErr(e instanceof Error ? e.message : "no result"));
  }, [runId]);

  if (err)
    return (
      <div className="shell">
        <Topbar meta="result" />
        <div className="main">
          <div className="err">{err}</div>
          <div className="actions">
            <Link className="btn" to="/">
              run another
            </Link>
          </div>
        </div>
      </div>
    );

  if (!r)
    return (
      <div className="shell">
        <Topbar meta="result" />
        <div className="main">
          <div className="center-note">
            loading result
            <span className="caret" />
          </div>
        </div>
      </div>
    );

  const b = r.budget ?? {};
  return (
    <div className="shell">
      <Topbar meta="result" />
      <div className="main">
        <div className="result-head">
          <h1 style={{ color: r.green ? "var(--accent)" : "var(--danger)" }}>
            {r.green ? "Green" : "No green"}
          </h1>
          <span className={`badge ${r.green ? "ok" : ""}`}>
            {r.green ? "verifier passed" : "budget stopped the run"}
          </span>
        </div>
        <div className="result-goal">{r.prompt}</div>

        <div className="card" data-testid="verdict">
          <div className="k">verdict — only the gauntlet declares success</div>
          <div className="row">
            <span>winner {r.winner}</span>
            <span
              style={{ color: r.green ? "var(--accent)" : "var(--danger)" }}
            >
              score {r.score?.toFixed(2)} · {r.reason}
            </span>
          </div>
          <div className="row">
            <span>
              {r.nodes ?? "?"} nodes explored
              {b.max_nodes != null && ` · ${b.nodes_used}/${b.max_nodes} budget`}
            </span>
            <span>${(b.usd_used ?? 0).toFixed(2)} spent</span>
          </div>
        </div>

        <div className="card" data-testid="gate">
          <div className="k">approval gate</div>
          <div className="row">
            {r.decision ? (
              <span
                style={{
                  color: r.decision.approved ? "var(--accent)" : "var(--danger)",
                }}
              >
                {r.decision.approved ? "approved" : "denied"} —{" "}
                {r.decision.reason}
              </span>
            ) : (
              <span>never reached — no green patch to ship</span>
            )}
          </div>
        </div>

        <div className="card" data-testid="receipts">
          <div className="k">receipt chain — ratchet audit</div>
          <pre className="audit">{r.audit || "no audit output"}</pre>
        </div>

        <QodoPanel />

        <div className="note">
          Everything above is real: the run was the actual search loop and
          verifier gauntlet executing offline against a seeded demo repo, the
          receipts were sealed and audited, and the Qodo findings are live
          review comments from this repository's pull requests.
        </div>

        <div className="actions">
          <Link className="btn" to="/">
            run another
          </Link>
        </div>
      </div>
    </div>
  );
}
