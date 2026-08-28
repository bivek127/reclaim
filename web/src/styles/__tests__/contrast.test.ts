import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Contrast is a property of the token file, so it is checked there rather than
 * by sampling rendered pixels: a token that fails here fails everywhere it is
 * used, and the check keeps working when components move.
 */

const css = readFileSync(resolve(process.cwd(), "src/styles/tokens.css"), "utf8");

const tokens: Record<string, string> = Object.fromEntries(
  [...css.matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{6});/g)].map((m) => [m[1]!, m[2]!]),
);

/** Fails loudly on a token that no longer exists, rather than reading undefined. */
function token(name: string): string {
  const value = tokens[name];
  if (!value) throw new Error(`token ${name} is not defined in tokens.css`);
  return value;
}

function channel(c: number): number {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as
    [number, number, number];
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as
    [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

const surface = () => token("--c-surface");
const canvas = () => token("--c-canvas");
const sunk = () => token("--c-surface-sunk");

/** [description, foreground token, background, minimum ratio] */
const TEXT: [string, string, () => string, number][] = [
  ["body text on surface", "--c-text", surface, 4.5],
  ["body text on canvas", "--c-text", canvas, 4.5],
  ["secondary text on surface", "--c-text-secondary", surface, 4.5],
  ["muted text on surface", "--c-text-muted", surface, 4.5],
  ["muted text on the sunk surface", "--c-text-muted", sunk, 4.5],
  ["advisory text on surface", "--c-text-faint", surface, 4.5],
  ["links on surface", "--c-primary-text", surface, 4.5],
  ["links on canvas", "--c-primary-text", canvas, 4.5],
];

const BADGES: [string, string, string][] = [
  ["success", "--c-success-text", "--c-success-tint"],
  ["attention", "--c-attention-text", "--c-attention-tint"],
  ["danger", "--c-danger-text", "--c-danger-tint"],
  ["progress", "--c-progress-text", "--c-progress-tint"],
  ["waiting", "--c-waiting-text", "--c-waiting-tint"],
  ["neutral", "--c-neutral-text", "--c-neutral-tint"],
];

describe("token contrast", () => {
  it.each(TEXT)("%s meets AA for normal text", (_label, fg, bg, min) => {
    expect(contrast(token(fg), bg())).toBeGreaterThanOrEqual(min);
  });

  it.each(BADGES)("the %s badge is readable on its own tint", (_label, fg, bg) => {
    expect(contrast(token(fg), token(bg))).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps button text readable on the primary fill", () => {
    expect(contrast(token("--c-text-inverse"), token("--c-primary")))
      .toBeGreaterThanOrEqual(4.5);
  });

  it("makes an empty input's boundary perceivable", () => {
    // A text field with no content is identified by its border alone, so the
    // border carries the 3:1 requirement for non-text UI.
    expect(contrast(token("--c-control-border"), surface()))
      .toBeGreaterThanOrEqual(3);
  });

  it("makes the focus ring visible against both page surfaces", () => {
    expect(contrast(token("--c-border-focus"), canvas())).toBeGreaterThanOrEqual(3);
    expect(contrast(token("--c-border-focus"), surface())).toBeGreaterThanOrEqual(3);
  });

  it("keeps the trust tiers ordered, so recession still reads as recession", () => {
    // Advisory must stay lighter than muted, which stays lighter than body.
    const body = luminance(token("--c-text"));
    const muted = luminance(token("--c-text-muted"));
    const faint = luminance(token("--c-text-faint"));
    expect(muted).toBeGreaterThan(body);
    expect(faint).toBeGreaterThan(muted);
  });
});
