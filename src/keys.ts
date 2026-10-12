export interface Key { name: string; text?: string; ctrl?: boolean; paste?: boolean }

/** Streaming escape decoder; pasted text never becomes command shortcuts. */
export class KeyDecoder {
  private pending = "";
  private pasting = false;
  private pasted = "";
  feed(text: string): Key[] {
    this.pending += text;
    const keys: Key[] = [];
    while (this.pending) {
      if (this.pasting) {
        const end = this.pending.indexOf("\x1b[201~");
        if (end < 0) {
          // Keep a possible split end marker for the next chunk.
          const safe = Math.max(0, this.pending.length - 5);
          this.pasted += this.pending.slice(0, safe); this.pending = this.pending.slice(safe); break;
        }
        this.pasted += this.pending.slice(0, end);
        keys.push({ name: "paste", text: this.pasted, paste: true });
        this.pending = this.pending.slice(end + 6); this.pasted = ""; this.pasting = false; continue;
      }
      if (this.pending.startsWith("\x1b[200~")) { this.pending = this.pending.slice(6); this.pasting = true; continue; }
      if (this.pending[0] === "\x1b") {
        if (this.pending.length === 1) break;
        if (this.pending[1] === "[" || this.pending[1] === "O") {
          const match = this.pending.match(/^\x1b(?:\[[0-9;?<]*[ -/]*[@-~]|O[A-Za-z])/);
          if (!match) break;
          const sequence = match[0]; this.pending = this.pending.slice(sequence.length);
          const names: Record<string, string> = { "A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end" };
          const tilde: Record<string, string> = { "1": "home", "3": "delete", "4": "end", "5": "pageup", "6": "pagedown", "7": "home", "8": "end" };
          const name = sequence.endsWith("~") ? tilde[sequence.slice(2, -1)] : names[sequence.at(-1)!];
          if (name) keys.push({ name });
          continue;
        }
        keys.push({ name: "escape" }); this.pending = this.pending.slice(1); continue;
      }
      const char = String.fromCodePoint(this.pending.codePointAt(0)!); this.pending = this.pending.slice(char.length);
      if (char === "\r" || char === "\n") keys.push({ name: "return" });
      else if (char === "\t") keys.push({ name: "tab" });
      else if (char === "\x7f" || char === "\b") keys.push({ name: "backspace" });
      else if (char.charCodeAt(0) < 32) keys.push({ name: String.fromCharCode(char.charCodeAt(0) + 96), ctrl: true });
      else keys.push({ name: char === " " ? "space" : "text", text: char });
    }
    return keys;
  }
  flush(): Key[] {
    if (this.pasting) return [];
    if (!this.pending) return [];
    const escape = this.pending[0] === "\x1b";
    this.pending = "";
    return escape ? [{ name: "escape" }] : [];
  }
}
