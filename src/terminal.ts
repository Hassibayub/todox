const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

/** Treat Markdown and registry content as text, never terminal escape sequences. */
export function clean(text: string): string {
  return Bun.stripANSI(text).replace(/[\x00-\x1f\x7f-\x9f]/g, " ");
}
export function cellWidth(text: string): number { return Bun.stringWidth(clean(text)); }
export function fit(text: string, columns: number): string {
  columns = Math.max(0, Math.floor(columns));
  text = clean(text);
  if (!columns) return "";
  if (cellWidth(text) <= columns) return text;
  let result = "";
  let width = 0;
  for (const { segment } of segmenter.segment(text)) {
    const size = cellWidth(segment);
    if (width + size > columns - 1) break;
    result += segment; width += size;
  }
  return result + "…";
}
export function pad(text: string, columns: number): string {
  const value = fit(text, columns);
  return value + " ".repeat(Math.max(0, columns - cellWidth(value)));
}
export function pack(left: string, right: string, columns: number): string {
  const tail = fit(right, Math.max(0, columns - 4));
  const leading = fit(left, Math.max(0, columns - cellWidth(tail) - 2));
  return leading + " ".repeat(Math.max(0, columns - cellWidth(leading) - cellWidth(tail))) + tail;
}
export function windowStart(index: number, length: number, height: number): number {
  return Math.max(0, Math.min(index - Math.floor(height / 2), length - height));
}
