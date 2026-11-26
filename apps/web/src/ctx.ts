import { createContext, useContext } from "react";
import type { Account, Catalog, GpuCardData, RuntimeView, RuntimesResponse, TaskDetail, TaskSummary } from "./types";

export type Route = "gpus" | "models" | "create" | "transcribe" | "dub" | "tts" | "tasks";

export interface ConfirmOptions {
  title: string;
  body?: string;
  ok?: string;
  danger?: boolean;
}

/** Where a task should run: a runtime id, or "auto" (reuse or start one of ``accelerator``). */
export interface Target {
  runtime: string | null;
  release: boolean;
}

export interface TaskRequest {
  kind: string;
  target: Target;
  accelerator: string;
  params: Record<string, unknown>;
  uploads?: string[];
}

export interface AppCtx {
  account: Account | null;
  catalog: Catalog | null;
  specs: GpuCardData[] | null;
  specsError: string | null;
  data: RuntimesResponse | null;
  runtimes: RuntimeView[];
  selected: string | null;
  setSelected: (id: string | null) => void;
  reloadRuntimes: () => Promise<void>;
  toast: (kind: "info" | "error" | "success", text: string) => void;
  handle: (e: unknown) => void;
  go: (route: Route) => void;
  startRuntime: (accelerator: string, shape: string, label: string) => Promise<void>;
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  activeTasks: TaskSummary[];
  submitTask: (req: TaskRequest) => Promise<TaskDetail | null>;
  retryTask: (id: string, overrides?: Record<string, unknown>) => Promise<TaskDetail | null>;
}

export const Ctx = createContext<AppCtx | null>(null);

export function useApp(): AppCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("AppCtx missing");
  return ctx;
}
