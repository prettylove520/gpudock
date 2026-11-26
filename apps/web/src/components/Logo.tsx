import { useState } from "react";

export function Logo({ size = 26, withText = false }: { size?: number; withText?: boolean }) {
  return (
    <span className="logo">
      <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
        <rect width="32" height="32" rx="9" fill="#111" />
        <rect x="8" y="9.5" width="16" height="13" rx="3" fill="none" stroke="#fff" strokeWidth="1.8" />
        <path d="M12 6.5v3M20 6.5v3M12 22.5v3M20 22.5v3" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" />
        <rect x="12" y="13" width="8" height="6" rx="1.5" fill="#45c24e" />
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
      <circle cx="30" cy="30" r="3" fill="var(--accent)" stroke="none" />
    </svg>
  );
}
