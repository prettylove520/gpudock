import { useState } from "react";

export function Logo({ size = 26, withText = false }: { size?: number; withText?: boolean }) {
  return (
    <span className="logo">
      <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
        <rect width="32" height="32" rx="7" fill="var(--ink)" />
        <rect x="8" y="10" width="16" height="12" rx="2" fill="none" stroke="var(--paper)" strokeWidth="2" />
        <path d="M11 7v3M16 7v3M21 7v3M11 22v3M16 22v3M21 22v3" stroke="var(--paper)" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="16" cy="16" r="2.4" fill="var(--signal)" />
      </svg>
      {withText && <span className="logo-text">GPUDock</span>}
    </span>
  );
}

const IMAGE_FOR: Record<string, string> = {
  T4: "hw-t4",
  L4: "hw-l4",
  A100: "hw-a100",
  G4: "hw-g4",
  H100: "hw-h100",
  V5E1: "hw-tpu",
  V6E1: "hw-tpu",
  NONE: "hw-cpu",
};

/** Product image for an accelerator; falls back to line art if the image is missing. */
export function HwImage({ accelerator, className = "" }: { accelerator: string; className?: string }) {
  const name = IMAGE_FOR[accelerator.toUpperCase()] ?? "hw-t4";
  return <Pic src={`/img/${name}.webp`} className={className} fallback={<ChipArt size={72} />} />;
}

export function Pic({ src, className = "", fallback, alt = "" }: { src: string; className?: string; fallback: React.ReactNode; alt?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className={`pic ${className}`}>
      {failed ? <div className="pic-fallback">{fallback}</div> : <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />}
    </div>
  );
}

/** Line-art chip used where no photograph is available. */
export function ChipArt({ size = 40 }: { size?: number }) {
  const pins = [14, 22, 30, 38, 46];
  return (
    <svg className="chip-art" width={size} height={size} viewBox="0 0 60 60" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6">
      {pins.map((p) => (
        <g key={p} strokeLinecap="round">
          <path d={`M${p} 4v6`} />
          <path d={`M${p} 50v6`} />
          <path d={`M4 ${p}h6`} />
          <path d={`M50 ${p}h6`} />
        </g>
      ))}
      <rect x="10" y="10" width="40" height="40" rx="5" />
      <rect x="20" y="20" width="20" height="20" rx="2" />
      <circle cx="30" cy="30" r="3" fill="var(--signal)" stroke="none" />
    </svg>
  );
}
