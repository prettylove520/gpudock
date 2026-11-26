import { useEffect, useRef } from "react";

/**
 * Call ``fn`` every ``ms`` while enabled. While the tab is hidden it pauses (a
 * tab left open overnight should not keep polling Colab), or slows down to
 * ``hiddenMs`` if given; it catches up at once when the tab is shown again.
 */
export function usePoll(fn: () => void | Promise<void>, ms: number, enabled = true, hiddenMs?: number) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    let timer: number | undefined;
    let running = false;
    const schedule = () => {
      if (!alive) return;
      if (!document.hidden) timer = window.setTimeout(tick, ms);
      else if (hiddenMs) timer = window.setTimeout(tick, hiddenMs);
    };
    const tick = async () => {
      window.clearTimeout(timer);
      if (running) return;
      if (document.hidden && !hiddenMs) return;
      running = true;
      try {
        await saved.current();
      } finally {
        running = false;
        schedule();
      }
    };
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [ms, enabled, hiddenMs]);
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

export function cu(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value <= 0) return "0 CU";
  return value < 0.01 ? "< 0.01 CU" : `${value.toFixed(value < 1 ? 2 : 1)} CU`;
}

/** A short "3 分钟前" style time. */
export function ago(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString();
}
