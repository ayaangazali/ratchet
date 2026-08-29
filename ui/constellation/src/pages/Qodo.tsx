import { useEffect, useState } from "react";
import { getQodo, getQodoFindings, requestQodoRereview } from "../api";
import Topbar from "../components/Topbar";
import type { QodoFeed, QodoPrFindings } from "../types";

/** /qodo — what the external reviewer told the agents to do, PR by PR. */
export default function Qodo() {
  const [feed, setFeed] = useState<QodoFeed | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [detail, setDetail] = useState<Record<number, QodoPrFindings>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    getQodo()
      .then((f) => {
        setFeed(f);
        console.info(`[qodo] status page: ${f.prs.length} PRs loaded`);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "qodo unavailable"));
  }, []);

  async function toggle(pr: number) {
    if (open === pr) {
      setOpen(null);
      return;
    }
    setOpen(pr);
    if (!detail[pr]) {
      setBusy(pr);
      try {
        const d = await getQodoFindings(pr);
        setDetail((prev) => ({ ...prev, [pr]: d }));
        console.info(
          `[qodo] PR #${pr}: ${d.findings.length} findings parsed, ` +
            `${d.replies.length} agent replies`,
        );
      } catch (e) {
        setErr(e instanceof Error ? e.message : "findings failed");
      } finally {
        setBusy(null);
      }
    }
  }

  async function rereview(pr: number) {
    try {
      const url = await requestQodoRereview(pr);
      console.info(`[qodo] fresh review commanded on PR #${pr}: ${url}`);
      setNote(`review requested on #${pr} — the bot posts its pass in ~2 min`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "re-review failed");
    }
  }

  return (
    <div className="shell">
      <Topbar meta="qodo status" />
      <div className="main">
        <div className="run-head">
          <h2 className="run-goal">Qodo — external review status</h2>
          <div className="run-sub">
            live from {feed?.repo ?? "github"} · what the reviewer told the
            agents to fix
          </div>
        </div>

        {note && <div className="detail">{note}</div>}
        {err && <div className="err">{err}</div>}
        {!feed && !err && (
          <div className="center-note">
            fetching qodo feed
            <span className="caret" />
          </div>
        )}

        {feed?.prs.map((pr) => {
          const review = [...pr.reviews]
            .filter((r) => r.kind === "review")
            .sort((a, b) => (a.at < b.at ? 1 : -1))[0];
          const counts = Object.entries(review?.counts ?? {}).filter(
            ([, n]) => n > 0,
          );
          const d = detail[pr.number];
          return (
            <div className="card" key={pr.number}>
              <button
                className="qodo-pr-head"
                onClick={() => toggle(pr.number)}
                data-testid={`qodo-pr-${pr.number}`}
              >
                <span className="qodo-title">
                  #{pr.number} {pr.title}
                </span>
                <span className="qodo-chips">
                  {counts.length === 0 && (
                    <span className="finding ok">clean</span>
                  )}
                  {counts.map(([name, n]) => (
                    <span className="finding" key={name}>
                      {n} {name}
                    </span>
                  ))}
                  <span className="qodo-at">{open === pr.number ? "▾" : "▸"}</span>
                </span>
              </button>

              {open === pr.number && (
                <div className="qodo-detail">
                  {busy === pr.number && (
                    <div className="detail">
                      parsing review
                      <span className="caret" />
                    </div>
                  )}
                  {d?.findings.map((f, i) => (
                    <div className="qodo-finding" key={`${i}-${f.title}`}>
                      <div className="label">
                        {f.n}. {f.title}
                      </div>
                      <div className="findings">
                        {f.tags.map((t) => (
                          <span className="finding bad" key={t}>
                            {t}
                          </span>
                        ))}
                      </div>
                      {f.description && (
                        <div className="detail">{f.description}</div>
                      )}
                      {f.agent_prompt && (
                        <details className="qodo-prompt">
                          <summary>what qodo told the agent</summary>
                          <pre className="diff">{f.agent_prompt}</pre>
                        </details>
                      )}
                    </div>
                  ))}
                  {d && d.findings.length === 0 && busy !== pr.number && (
                    <div className="detail">no findings in the latest review</div>
                  )}
                  {d && d.replies.length > 0 && (
                    <div className="qodo-replies">
                      <div className="k">agent replies</div>
                      {d.replies.map((r, i) => (
                        <div className="detail" key={i}>
                          <b>{r.author}</b> — {r.text}
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="actions">
                    <button className="btn" onClick={() => rereview(pr.number)}>
                      re-review now
                    </button>{" "}
                    <a
                      className="btn"
                      href={pr.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      open on github
                    </a>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
