import type { Budget, RunResult } from "./types";

// No gateway on :8080 in this vendored copy — mock the whole API so the
// create → pipeline → result flow (and its animations) runs standalone.
// Flip to false when a real gateway is available.
const MOCK = true;

export async function createRun(
  prompt: string,
  budget: Budget,
): Promise<string> {
  if (MOCK) return mockCreateRun(prompt, budget);
  const res = await fetch("/api/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, budget }),
  });
  if (!res.ok) throw new Error(`create failed (${res.status})`);
  const data = await res.json();
  return data.run_id as string;
}

export async function getResult(runId: string): Promise<RunResult> {
  if (MOCK) return mockGetResult(runId);
  const res = await fetch(`/api/result/${runId}`);
  if (!res.ok) throw new Error(`result not ready (${res.status})`);
  return (await res.json()) as RunResult;
}

export type StreamHandlers = {
  onStart?: (d: { run_id: string; slug: string; total: number }) => void;
  onStage?: (d: any) => void;
  onLog?: (d: { key: string; line: string }) => void;
  onDone?: (d: { result: RunResult }) => void;
  onError?: (e: unknown) => void;
};

/** Subscribe to the SSE pipeline feed. Returns a cleanup fn. */
export function streamRun(runId: string, h: StreamHandlers): () => void {
  if (MOCK) return mockStreamRun(runId, h);
  const es = new EventSource(`/api/stream/${runId}`);
  es.addEventListener("start", (e) =>
    h.onStart?.(JSON.parse((e as MessageEvent).data)),
  );
  es.addEventListener("stage", (e) =>
    h.onStage?.(JSON.parse((e as MessageEvent).data)),
  );
  es.addEventListener("log", (e) =>
    h.onLog?.(JSON.parse((e as MessageEvent).data)),
  );
  es.addEventListener("done", (e) => {
    h.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = (e) => h.onError?.(e);
  return () => es.close();
}

// ---------------------------------------------------------------------------
// Mock backend: timer-driven stand-in for the gateway's SSE pipeline.

interface MockRun {
  prompt: string;
  budget: Budget;
  slug: string;
}

const mockRuns = new Map<string, MockRun>();

function slugify(s: string): string {
  return (
    s
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 32) || "untitled"
  );
}

async function mockCreateRun(prompt: string, budget: Budget): Promise<string> {
  const runId = `mock-${Date.now().toString(36)}`;
  mockRuns.set(runId, { prompt, budget, slug: slugify(prompt) });
  await new Promise((r) => setTimeout(r, 400));
  return runId;
}

const MOCK_STAGES: { key: string; label: string; detail: string; layer: string; logs: string[] }[] = [
  { key: "plan", label: "Planning build", detail: "decompose prompt into build plan", layer: "spine", logs: ["parsing prompt", "6 components identified", "plan locked"] },
  { key: "scaffold", label: "Scaffolding project", detail: "repo layout, tooling, CI skeleton", layer: "harness", logs: ["git init", "vite + react template", "ci workflow written"] },
  { key: "codegen", label: "Generating code", detail: "components, routes, state", layer: "spine", logs: ["src/App.tsx", "src/routes/notes.tsx", "src/state/store.ts"] },
  { key: "deps", label: "Resolving dependencies", detail: "lockfile solve + install", layer: "sandbox", logs: ["resolving 214 packages", "fetching…", "install ok"] },
  { key: "build", label: "Building", detail: "typecheck + bundle", layer: "sandbox", logs: ["tsc -b", "vite build", "dist 148 kB gzip"] },
  { key: "tests", label: "Running tests", detail: "unit + integration", layer: "eval", logs: ["34 unit", "8 integration", "all green"] },
  { key: "verify", label: "Golden-test verification", detail: "held-out eval against spec", layer: "eval", logs: ["12 golden cases", "12/12 passed"] },
  { key: "provision", label: "Provisioning infra", detail: "accounts, db, secrets (dry run)", layer: "platform", logs: ["supabase project (stub)", "vercel project (stub)"] },
  { key: "deploy", label: "Deploying", detail: "push + release (dry run)", layer: "platform", logs: ["pushed main", "release created"] },
];

const STAGE_MS = 1400;
const LOG_MS = 400;

function mockResult(runId: string): RunResult {
  const run = mockRuns.get(runId) ?? {
    prompt: "a realtime markdown notes app with auth",
    budget: { mode: "free" as const, min: 0, max: 0 },
    slug: "realtime-markdown-notes",
  };
  return {
    run_id: runId,
    prompt: run.prompt,
    slug: run.slug,
    budget: run.budget,
    dry_run: true,
    deploy_url: `https://${run.slug}.vercel.app`,
    repo_url: `https://github.com/constellation-builds/${run.slug}`,
    verification: { task: "held-out golden tests", ok: true, status: "passed", passed: 12, total: 12, cost_usd: 0.42 },
    credentials: [
      { platform: "supabase", email: `${run.slug}@builds.constellation.dev`, password: "••••••••••••", status: "stub (dry run)" },
      { platform: "vercel", email: `${run.slug}@builds.constellation.dev`, password: "••••••••••••", status: "stub (dry run)" },
    ],
  };
}

function mockStreamRun(runId: string, h: StreamHandlers): () => void {
  const timers: ReturnType<typeof setTimeout>[] = [];
  const at = (ms: number, fn: () => void) => timers.push(setTimeout(fn, ms));
  const run = mockRuns.get(runId);
  const total = MOCK_STAGES.length;

  at(50, () =>
    h.onStart?.({ run_id: runId, slug: run?.slug ?? "demo", total }),
  );

  MOCK_STAGES.forEach((s, i) => {
    const start = 300 + i * STAGE_MS;
    at(start, () =>
      h.onStage?.({ index: i, total, key: s.key, label: s.label, detail: s.detail, layer: s.layer, status: "active", stub: false }),
    );
    s.logs.forEach((line, j) =>
      at(start + 150 + j * LOG_MS, () => h.onLog?.({ key: s.key, line })),
    );
    at(start + STAGE_MS - 100, () =>
      h.onStage?.({ index: i, total, key: s.key, label: s.label, detail: s.detail, layer: s.layer, status: "done", stub: false }),
    );
  });

  at(300 + total * STAGE_MS + 300, () =>
    h.onDone?.({ result: mockResult(runId) }),
  );

  return () => timers.forEach(clearTimeout);
}

async function mockGetResult(runId: string): Promise<RunResult> {
  await new Promise((r) => setTimeout(r, 250));
  return mockResult(runId);
}
