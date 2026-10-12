import { expect, test } from "bun:test";
import { RGBA } from "@opentui/core";
import { terminalTheme } from "../src/theme";

test("resolves reported terminal colors as opaque RGB on light and dark themes", () => {
  for (const [bg, fg] of [["#101010", "#dedede"], ["#fafafa", "#222222"]]) {
    const theme = terminalTheme({ defaultBackground: bg, defaultForeground: fg, palette: [null, "#cc2222"] });
    expect(theme.bg.equals(RGBA.fromHex(bg!))).toBe(true);
    expect(theme.ink.equals(RGBA.fromHex(fg!))).toBe(true);
    expect(theme.bg.a).toBe(1);
    expect(theme.bg.intent).toBe("rgb");
    expect(theme.red.equals(RGBA.fromHex("#cc2222"))).toBe(true);
  }
});

test("missing terminal replies use readable mode-aware fallback colors", () => {
  expect(terminalTheme().bg.equals(RGBA.fromHex("#111b22"))).toBe(true);
  const light = terminalTheme(undefined, "light");
  expect(light.bg.equals(RGBA.fromHex("#ffffff"))).toBe(true);
  expect(light.ink.equals(RGBA.fromHex("#202020"))).toBe(true);
});
