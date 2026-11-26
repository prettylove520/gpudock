import { createContext, useContext } from "react";
import type { Account, Catalog, GpuCardData, RuntimeView, RuntimesResponse } from "./types";

export type Route = "gpus" | "models" | "create" | "transcribe" | "dub" | "tts" | "tasks";

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
}

export const Ctx = createContext<AppCtx | null>(null);

export function useApp(): AppCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("AppCtx missing");
  return ctx;
}
