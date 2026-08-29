export type BudgetMode = "free" | "budget";

export interface Budget {
  mode: BudgetMode;
  min: number;
  max: number;
}

/** One attempt row in the pipeline — a node the search graded. */
export interface StageRow {
  index: number;
  key: string;
  label: string;
  detail: string;
  layer: string;
  status: "active" | "done" | "green" | "pruned";
  score?: number;
  outcome?: string;
  findings?: string[];
  reason?: string;
}

export interface ApprovalRequest {
  id: string;
  summary: string;
  stats: {
    nodes_explored?: number;
    path_length?: number;
    score?: number;
    green?: boolean;
    cost_usd?: number;
  };
  diff_preview: string;
}

export interface RunResult {
  run_id: string;
  prompt: string;
  scenario: string;
  green: boolean;
  winner: string;
  score: number;
  reason: string;
  nodes: number;
  budget: {
    nodes_used?: number;
    max_nodes?: number;
    elapsed?: number;
    usd_used?: number;
    max_usd?: number;
  };
  decision?: { approved: boolean; reason: string } | null;
  audit?: string;
}

export interface QodoReview {
  kind: "review" | "summary" | "comment";
  counts: Record<string, number>;
  at: string;
}

export interface QodoPr {
  number: number;
  title: string;
  state: string;
  url: string;
  reviews: QodoReview[];
}

export interface QodoFinding {
  n: number;
  title: string;
  tags: string[];
  description: string;
  agent_prompt: string;
}

export interface QodoPrFindings {
  pr: number;
  reviewed_at: string | null;
  findings: QodoFinding[];
  replies: { author: string; at: string; text: string }[];
}

export interface QodoFeed {
  fetched_at: number;
  repo: string;
  prs: QodoPr[];
  stale?: boolean;
  error?: string;
}
