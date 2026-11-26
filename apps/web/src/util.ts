import { useEffect, useRef } from "react";

export function usePoll(fn: () => void | Promise<void>, ms: number, enabled = true) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        await saved.current();
      } finally {
        if (alive) timer = window.setTimeout(tick, ms);
      }
    };
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [ms, enabled]);
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h} 小时 ${m} 分`;
  if (m > 0) return `${m} 分 ${s % 60} 秒`;
  return `${s} 秒`;
}

export function tierLabel(tier: string): string {
  return (
    {
      SUBSCRIPTION_TIER_FREE: "免费版",
      SUBSCRIPTION_TIER_PRO: "Pro",
      SUBSCRIPTION_TIER_PRO_PLUS: "Pro+",
    } as Record<string, string>
  )[tier] ?? "未知";
}

export function vram(gb: number | null | undefined): string {
  if (gb == null) return "—";
  return `${gb % 1 === 0 ? gb : gb.toFixed(1)} GB`;
}

export function rate(cu: number | null | undefined): string {
  return cu == null ? "单价未知" : `约 ${cu} CU/小时`;
}
