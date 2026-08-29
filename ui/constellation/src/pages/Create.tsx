import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createRun } from "../api";
import Topbar from "../components/Topbar";
import type { BudgetMode } from "../types";

// Each chip drives a different scripted scenario through the real verifier.
const SCENARIO_CHIPS = [
  { label: "honest fix", prompt: "fix the slugify bug" },
  { label: "explore first", prompt: "explore two approaches to the slugify bug" },
  { label: "cheating patch", prompt: "try a cheat: hardcode the failing cases" },
  { label: "canary hack", prompt: "special-case the canary sample inputs" },
  { label: "exhaust budget", prompt: "exhaust the budget with bad patches" },
];

export default function Create() {
  const nav = useNavigate();
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<BudgetMode>("free");
  const [max, setMax] = useState("6");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit(text?: string) {
    const p = (text ?? prompt).trim();
    if (!p || busy) return;
    setBusy(true);
    setErr("");
    try {
      const budget = {
        mode,
        min: 0,
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
      <Topbar meta="new run" />
      <div className="create-wrap">
        <div className="create-inner">
          <div className="create-label">give the search a goal</div>
          <input
            className="create-input"
            autoFocus
            placeholder="fix the slugify bug…"
            value={prompt}
            disabled={busy}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />

          <div className="chips">
            {SCENARIO_CHIPS.map((c) => (
              <button
                key={c.label}
                className="chip"
                disabled={busy}
                onClick={() => {
                  setPrompt(c.prompt);
                  submit(c.prompt);
                }}
              >
                {c.label}
              </button>
            ))}
          </div>

          <div className="budget-row">
            <div className="seg" role="group" aria-label="node budget">
              <button
                aria-pressed={mode === "free"}
                onClick={() => setMode("free")}
              >
                default budget
              </button>
              <button
                aria-pressed={mode === "budget"}
                onClick={() => setMode("budget")}
              >
                cap nodes
              </button>
            </div>

            {mode === "budget" && (
              <span className="amount">
                <span>max</span>
                <input
                  inputMode="numeric"
                  value={max}
                  onChange={(e) => setMax(e.target.value)}
                  aria-label="max nodes"
                />
              </span>
            )}
          </div>

          <div className="hint">
            {busy ? (
              <>
                seeding repo + starting the search
                <span className="caret" />
              </>
            ) : (
              <>
                press <kbd>enter</kbd> to run · every run is the real verifier,
                offline
              </>
            )}
          </div>
          {err && <div className="err">{err}</div>}
        </div>
      </div>
    </div>
  );
}
