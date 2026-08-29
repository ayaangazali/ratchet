import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getResult } from "../api";
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
              create another
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

  const v = r.verification;
  const verifyOk = (v?.total ?? 0) > 0 && v.passed === v.total;

  return (
    <div className="shell">
      <Topbar meta="result" />
      <div className="main">
        <div className="result-head">
          <h1>Deployed</h1>
          {r.dry_run && <span className="badge">dry run</span>}
        </div>
        <div className="result-goal">{r.prompt}</div>

        <div className="card">
          <div className="k">deployed link</div>
          <a
            className="big-link"
            href={r.deploy_url}
            target="_blank"
            rel="noreferrer"
          >
            {r.deploy_url}
          </a>
        </div>

        <div className="card">
          <div className="k">github repository</div>
          <a
            className="big-link"
            href={r.repo_url}
            target="_blank"
            rel="noreferrer"
          >
            {r.repo_url}
          </a>
        </div>

        <div className="card">
          <div className="k">verification — golden tests (real)</div>
          <div className="row">
            <span>{v?.task ?? "held-out eval"}</span>
            <span
              style={{ color: verifyOk ? "var(--accent)" : "var(--danger)" }}
            >
              {v?.passed ?? 0}/{v?.total ?? 0} passed · {v?.status ?? "n/a"}
            </span>
          </div>
        </div>

        <div className="card">
          <div className="k">project credentials — accounts registered</div>
          <table className="cred-table">
            <thead>
              <tr>
                <th>platform</th>
                <th>email</th>
                <th>password</th>
                <th>status</th>
              </tr>
            </thead>
            <tbody>
              {r.credentials.map((c) => (
                <tr key={c.platform}>
                  <td>{c.platform}</td>
                  <td>{c.email}</td>
                  <td>{c.password}</td>
                  <td className="status">{c.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {r.dry_run && (
          <div className="note">
            Phase 0 dry run. The build + golden-test verification above is real;
            account registration, infra provisioning and deploy are typed seams
            (forbidden working impls this phase) — no real account was created
            and nothing was deployed. Budget: {r.budget.mode}
            {r.budget.mode === "budget"
              ? ` ($${r.budget.min}–$${r.budget.max})`
              : ""}
            .
          </div>
        )}

        <div className="actions">
          <Link className="btn" to="/">
            create another
          </Link>
        </div>
      </div>
    </div>
  );
}
