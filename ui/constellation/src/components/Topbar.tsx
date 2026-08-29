import { Link } from "react-router-dom";

export default function Topbar({ meta }: { meta?: string }) {
  return (
    <div className="topbar">
      <Link to="/" className="wordmark">
        RATCHET<span className="dot">.</span>
      </Link>
      <div className="topbar-right">
        <Link to="/qodo" className="meta">
          /qodo
        </Link>
        <div className="meta">{meta ?? "tests decide, not the agent"}</div>
      </div>
    </div>
  );
}
