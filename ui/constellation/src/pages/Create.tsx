import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createRun } from "../api";
import Topbar from "../components/Topbar";
import type { BudgetMode } from "../types";

export default function Create() {
  const nav = useNavigate();
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<BudgetMode>("free");
  const [min, setMin] = useState("5");
  const [max, setMax] = useState("50");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    const p = prompt.trim();
    if (!p || busy) return;
    setBusy(true);
    setErr("");
    try {
      const budget = {
        mode,
        min: mode === "budget" ? Number(min) || 0 : 0,
        max: mode === "budget" ? Number(max) || 0 : 0,
      };
      const runId = await createRun(p, budget);
      nav(`/run/${runId}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "could not start run");
      setBusy(false);
    }
  }

  return (
    <div className="shell">
      <Topbar meta="new build" />
      <div className="create-wrap">
        <div className="create-inner">
          <div className="create-label">create anything</div>
          <input
            className="create-input"
            autoFocus
            placeholder="a realtime markdown notes app with auth…"
            value={prompt}
            disabled={busy}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />

          <div className="budget-row">
            <div className="seg" role="group" aria-label="budget mode">
              <button
                aria-pressed={mode === "free"}
                onClick={() => setMode("free")}
              >
                free
              </button>
              <button
                aria-pressed={mode === "budget"}
                onClick={() => setMode("budget")}
              >
                budget
              </button>
            </div>

            {mode === "budget" && (
              <>
                <span className="amount">
                  <span>$</span>
                  <input
                    inputMode="decimal"
                    value={min}
                    onChange={(e) => setMin(e.target.value)}
                    aria-label="min budget"
                  />
                </span>
                <span className="dash">—</span>
                <span className="amount">
                  <span>$</span>
                  <input
                    inputMode="decimal"
                    value={max}
                    onChange={(e) => setMax(e.target.value)}
                    aria-label="max budget"
                  />
                </span>
              </>
            )}
          </div>

          <div className="hint">
            {busy ? (
              <>
                starting
                <span className="caret" />
              </>
            ) : (
              <>
                press <kbd>enter</kbd> to build
              </>
            )}
          </div>
          {err && <div className="err">{err}</div>}
        </div>
      </div>
    </div>
  );
}
