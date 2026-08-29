export type BudgetMode = "free" | "budget";

export interface Budget {
  mode: BudgetMode;
  min: number;
  max: number;
}

export interface StageEvent {
  index: number;
  total: number;
  key: string;
  label: string;
  detail: string;
  layer: string;
  status: "active" | "done";
  stub: boolean;
}

export interface Verification {
  task?: string;
  ok?: boolean;
  status?: string;
  passed: number;
  total: number;
  cost_usd?: number;
  error?: string;
}

export interface Credential {
  platform: string;
  email: string;
  password: string;
  status: string;
}

export interface RunResult {
  run_id: string;
  prompt: string;
  slug: string;
  budget: Budget;
  dry_run: boolean;
  deploy_url: string;
  repo_url: string;
  verification: Verification;
  credentials: Credential[];
}
