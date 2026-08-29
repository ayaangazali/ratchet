import { useEffect, useState } from "react";
import { getQodo, requestQodoRereview } from "../api";
import type { QodoFeed } from "../types";

/** Live findings the qodo-code-review bot left on this repo's real PRs. */
export default function QodoPanel() {
  const [feed, setFeed] = useState<QodoFeed | null>(null);
  const [err, setErr] = useState("");
  const [requested, setRequested] = useState("");

  async function rereview(pr: number) {
    if (requested) return;
    try {
      const url = await requestQodoRereview(pr);
      console.info(`[qodo] fresh review commanded on PR #${pr}: ${url}`);
      setRequested(
        `review requested on #${pr} — the bot posts its pass in ~2 min`,
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "re-review failed");
    }
  }

  useEffect(() => {
    getQodo()
      .then((f) => {
        setFeed(f);
        const reviews = f.prs.reduce((n, p) => n + p.reviews.length, 0);
        console.info(
          `[qodo] live feed: ${f.prs.length} PRs, ${reviews} bot comments, ` +
            `fetched ${new Date(f.fetched_at * 1000).toISOString()}` +
            (f.stale ? " (stale cache)" : ""),
        );
        const latest = f.prs
          .flatMap((p) => p.reviews.map((r) => ({ pr: p.number, ...r })))
          .sort((a, b) => (a.at < b.at ? 1 : -1))[0];
        if (latest)
          console.info(
            `[qodo] newest review: PR #${latest.pr} at ${latest.at} — ` +
              JSON.stringify(latest.counts),
          );
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "qodo unavailable"));
  }, []);

  return (
    <div className="card qodo" data-testid="qodo-panel">
      <div className="k">
        external reviewer — qodo · live from {feed?.repo ?? "github"}
        {feed?.stale && " (cached)"}
      </div>
      {requested && <div className="detail">{requested}</div>}
      {err && <div className="err">{err}</div>}
      {!feed && !err && (
        <div className="detail">
          fetching qodo reviews
          <span className="caret" />
        </div>
      )}
      {feed &&
        feed.prs.map((pr) => {
          // newest review comment wins — a re-triggered /review supersedes the old one
          const review = [...pr.reviews]
            .filter((r) => r.kind === "review")
            .sort((a, b) => (a.at < b.at ? 1 : -1))[0];
          const counts = review?.counts ?? {};
          const entries = Object.entries(counts).filter(([, n]) => n > 0);
          return (
            <div className="qodo-row" key={pr.number}>
              <a href={pr.url} target="_blank" rel="noreferrer">
                #{pr.number}
              </a>
              <span className="qodo-title">{pr.title}</span>
              <span className="qodo-chips">
                {entries.length === 0 && (
                  <span className="finding ok">clean</span>
                )}
                {entries.map(([name, n]) => (
                  <span className="finding" key={name}>
                    {n} {name}
                  </span>
                ))}
                {review && (
                  <span className="qodo-at">
                    {new Date(review.at).toLocaleString(undefined, {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                )}
                <button
                  className="chip qodo-go"
                  title="command a fresh Qodo review of this PR"
                  disabled={!!requested}
                  onClick={() => rereview(pr.number)}
                  data-testid={`qodo-rereview-${pr.number}`}
                >
                  ↻
                </button>
              </span>
            </div>
          );
        })}
      {feed && feed.prs.length === 0 && (
        <div className="detail">{feed.error ?? "no qodo reviews found"}</div>
      )}
    </div>
  );
}
