import type { Budget, RunResult } from "./types";

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

export type StreamHandlers = {
  onStart?: (d: { run_id: string; slug: string; total: number }) => void;
  onStage?: (d: any) => void;
  onLog?: (d: { key: string; line: string }) => void;
  onDone?: (d: { result: RunResult }) => void;
  onError?: (e: unknown) => void;
};

/** Subscribe to the SSE pipeline feed. Returns a cleanup fn. */
export function streamRun(runId: string, h: StreamHandlers): () => void {
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
