import { RGBA, type TerminalColors } from "@opentui/core";

/** Resolve opaque colors so blank-cell erasure cannot inherit inverse/default state. */
export function terminalTheme(palette?: Partial<TerminalColors>, mode?: "dark" | "light" | null) {
  const light = mode === "light";
  const bg = RGBA.fromHex(palette?.defaultBackground || (light ? "#ffffff" : "#111b22"));
  const ink = RGBA.fromHex(palette?.defaultForeground || (light ? "#202020" : "#e6ede9"));
  const accent = (slot: number) => palette?.palette?.[slot] ? RGBA.fromHex(palette.palette[slot]!) : RGBA.fromIndex(slot);
  return { bg, ink, muted: ink, cyan: accent(6), green: accent(2), amber: accent(3), red: accent(1) };
}
