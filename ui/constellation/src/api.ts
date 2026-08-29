import type {
  ApprovalRequest,
  Budget,
  QodoFeed,
  RunResult,
  StageRow,
} from "./types";

export async function createRun(
  prompt: string,
  budget: Budget,
): Promise<string> {
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
  const res = await fetch(`/api/result/${runId}`);
  if (!res.ok) throw new Error(`result not ready (${res.status})`);
  return (await res.json()) as RunResult;
}

export async function approveRun(
  runId: string,
  allow: boolean,
): Promise<void> {
  const res = await fetch(`/api/approve/${runId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ allow, reason: allow ? "approved in ui" : "denied in ui" }),
  });
  if (!res.ok) throw new Error(`approve failed (${res.status})`);
}

export async function getQodo(): Promise<QodoFeed> {
  const res = await fetch("/api/qodo");
  if (!res.ok) throw new Error(`qodo feed failed (${res.status})`);
  return (await res.json()) as QodoFeed;
}

export type StreamHandlers = {
  onStart?: (d: {
    run_id: string;
    slug: string;
    total: number;
    scenario: string;
  }) => void;
  onStage?: (d: StageRow) => void;
  onLog?: (d: { key: string; line: string }) => void;
  onApproval?: (d: ApprovalRequest) => void;
  onResolved?: (d: { approved: boolean; reason: string }) => void;
  onDone?: (d: { result: RunResult }) => void;
  onError?: (e: unknown) => void;
};

/** Subscribe to the SSE pipeline feed. Returns a cleanup fn. */
export function streamRun(runId: string, h: StreamHandlers): () => void {
  const es = new EventSource(`/api/stream/${runId}`);
  const on = (name: string, fn?: (d: any) => void) =>
    es.addEventListener(name, (e) => fn?.(JSON.parse((e as MessageEvent).data)));
  on("start", h.onStart);
  on("stage", h.onStage);
  on("log", h.onLog);
  on("approval", h.onApproval);
  on("resolved", h.onResolved);
  on("done", h.onDone);
  es.addEventListener("eof", () => es.close());
  es.onerror = (e) => h.onError?.(e);
  return () => es.close();
}
