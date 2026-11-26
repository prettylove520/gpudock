export interface LoginState {
  status: "idle" | "running" | "done" | "error";
  url: string | null;
  error: string | null;
}

export interface AuthStatus {
  logged_in: boolean;
  mode: string;
  client_ready: boolean;
  client_error: string | null;
  login: LoginState;
}

export interface Account {
  tier: string;
  email: string | null;
}

export interface SpecOption {
  shape: string;
  label: string;
  detail: string;
  vram_gb: number | null;
  cu_per_hour: number | null;
  eligible: boolean;
}

export interface GpuCardData {
  accelerator: string;
  variant: string;
  name: string;
  kind: "gpu" | "tpu" | "cpu";
  vram_gb: number | null;
  arch: string;
  cu_per_hour: number | null;
  tagline: string;
  good_for: string[];
  can_run: string[];
  cannot_run: string[];
  notes: string;
  eligible: boolean;
  options: SpecOption[];
}

export interface RuntimeView {
  name: string;
  id: string;
  accelerator: string;
  shape: string;
  label: string;
  display_name: string;
  vram_gb: number | null;
  cu_per_hour: number | null;
  owned: boolean;
  auto: boolean;
  created_at: number | null;
  uptime_s: number | null;
  est_cu: number | null;
  busy: boolean;
  busy_exec: string | null;
  busy_task: string | null;
  idle_s: number;
  auto_release_in_s: number | null;
}

export interface Job {
  id: string;
  kind: string;
  label: string;
  accelerator: string;
  shape: string;
  status: "running" | "done" | "error" | "cancelled";
  message: string;
  error: string | null;
  result: { name: string; id: string } | null;
  attempts?: number;
  auto?: boolean;
  started: number;
  finished: number | null;
}

export interface Usage {
  today_cu: number;
  month_cu: number;
  has_unpriced: boolean;
}

export interface RuntimesResponse {
  runtimes: RuntimeView[];
  pending: Job[];
  auto_release_minutes: number;
  release_on_exit: boolean;
  usage: Usage;
}

export interface Chunk {
  i: number;
  kind: "stdout" | "stderr" | "result" | "display" | "error" | "system";
  text: string;
}

export interface ExecView {
  id: string;
  runtime: string;
  status: "running" | "ok" | "error" | "aborted";
  chunks: Chunk[];
  next: number;
  truncated?: boolean;
  error: { ename: string; evalue: string; traceback: string[] } | null;
  started: number;
  finished: number | null;
}

export interface Stats {
  gpu?: { name: string; util: number; mem_used_mb: number; mem_total_mb: number; temp_c: number };
  gpu_error?: string;
  ram?: { total_gb: number; used_gb: number };
  disk?: { total_gb: number; used_gb: number };
}

export interface AppEvent {
  i: number;
  t: number;
  kind: string;
  message: string;
  runtime?: string;
  task?: string;
}

export interface Recipe {
  name: string;
  engine: string;
  category: "asr" | "audio" | "tts" | "llm" | "engine";
  summary: string;
  license: string;
  commercial: string;
  vram_gb: number;
  download: string;
  install_minutes: number;
  models?: string[];
  note?: string;
  min_ram_gb?: number;
  homepage: string;
}

export interface LangInfo {
  name: string;
  kokoro: string | null;
}

export interface Catalog {
  recipes: Record<string, Recipe>;
  langs: Record<string, LangInfo>;
  kokoro_voices: Record<string, { id: string; label: string }[]>;
  gen_models: Record<string, GenModel>;
  mode_labels: Record<string, string>;
  llm_models: LlmModel[];
}

export interface AgentStatus {
  agent?: string;
  python?: string;
  gpu?: string | null;
  installed?: Record<string, { at: number; model?: string; models?: string[]; templates?: string[] }>;
  busy?: boolean;
}

export interface UploadInfo {
  id: string;
  name: string;
  size: number;
}

export interface TaskOutput {
  name: string;
  size: number;
  kind: "video" | "audio" | "image" | "subtitle" | "text" | "data" | "file";
  url: string;
}

export interface GenBrief {
  model: string;
  mode: string;
  prompt: string;
  width: number;
  height: number;
  seconds?: number | null;
  fast?: boolean | null;
  seed?: number | null;
}

export interface TaskSummary {
  id: string;
  kind: string;
  kind_label: string;
  title: string;
  runtime: string;
  status: "queued" | "running" | "waiting_review" | "done" | "error" | "cancelled" | "interrupted";
  stage: string;
  progress: number;
  error: string | null;
  created: number;
  started: number | null;
  finished: number | null;
  outputs: TaskOutput[];
  out_dir: string;
  result: { reply?: string; model?: string; recipe?: string; seed?: number; gen_seconds?: number | null };
  accelerator: string | null;
  cu_per_hour: number | null;
  est_cu: number | null;
  release_after: boolean;
  retry_of: string | null;
  can_retry: boolean;
  gen?: GenBrief;
}

export interface ReviewSegment {
  id: number;
  start: number;
  end: number;
  source: string;
  text: string;
}

export interface TaskDetail extends TaskSummary {
  params: Record<string, unknown>;
  items: { name: string; status: string; detail: string }[];
  logs: string[];
  log_next: number;
  review: { items: { name: string; segments: ReviewSegment[] }[] } | null;
}

export interface GenModel {
  name: string;
  kind: "video" | "image";
  vendor: string;
  summary: string;
  license: string;
  commercial: "yes" | "conditional" | "unknown" | "no";
  license_note?: string;
  gated?: string;
  templates: Record<string, string>;
  gpus: string[];
  vram_gb: number;
  download_gb: number;
  fps?: number;
  max_seconds?: number;
  size: [number, number];
  hd_size?: [number, number];
  fast?: string;
  experimental?: boolean;
}

export interface LlmModel {
  id: string;
  name: string;
  vram_gb: number;
  note: string;
}
