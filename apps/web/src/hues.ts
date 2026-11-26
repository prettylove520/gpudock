// A muted colour code per accelerator (like cable colour codes): used for the
// thin index stripe on cards and status chips, never for glows or fills.
export const HUES: Record<string, string> = {
  T4: "#6f8a64",
  L4: "#3f7d83",
  A100: "#a7843f",
  G4: "#4b4945",
  H100: "#b4553b",
  V5E1: "#5b6f97",
  V6E1: "#4f5f8f",
  NONE: "#8a847a",
};

export function hueOf(accelerator: string): string {
  return HUES[accelerator.toUpperCase()] ?? "#4b4945";
}

export function hueStyle(accelerator: string): React.CSSProperties {
  return { ["--hue" as string]: hueOf(accelerator) };
}
