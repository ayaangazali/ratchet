import { useEffect, useState } from "react";
import { getQodo } from "../api";
import type { QodoFeed } from "../types";

/** Live findings the qodo-code-review bot left on this repo's real PRs. */
export default function QodoPanel() {
  const [feed, setFeed] = useState<QodoFeed | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    getQodo()
      .then(setFeed)
      .catch((e) => setErr(e instanceof Error ? e.message : "qodo unavailable"));
  }, []);

  return (
    <div className="card qodo" data-testid="qodo-panel">
      <div className="k">
        external reviewer — qodo · live from {feed?.repo ?? "github"}
        {feed?.stale && " (cached)"}
      </div>
      {err && <div className="err">{err}</div>}
      {!feed && !err && (
        <div className="detail">
          fetching qodo reviews
          <span className="caret" />
        </div>
      )}
      {feed &&
        feed.prs.map((pr) => {
          const review = pr.reviews.find((r) => r.kind === "review");
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
