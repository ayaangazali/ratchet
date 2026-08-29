import { Link } from "react-router-dom";

export default function Topbar({ meta }: { meta?: string }) {
  return (
    <div className="topbar">
      <Link to="/" className="wordmark">
        CONSTELLATION<span className="dot">.</span>
      </Link>
      <div className="meta">{meta ?? "autonomous build"}</div>
    </div>
  );
}
