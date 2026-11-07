import { describe, it, expect } from "vitest";
import postcss from "postcss";
import scalablePx, { isScalableProp, pxToRem } from "./postcss-scalable-px.js";

const run = (css, from = "/app/frontend/src/index.css") =>
  postcss([scalablePx()]).process(css, { from }).css;

describe("pxToRem", () => {
  it("converts px lengths at 16px = 1rem", () => {
    expect(pxToRem("10px")).toBe("0.625rem");
    expect(pxToRem("10.5px")).toBe("0.65625rem");
    expect(pxToRem("0 2px")).toBe("0 0.125rem");
    expect(pxToRem("calc(50% + 6px)")).toBe("calc(50% + 0.375rem)");
    expect(pxToRem("-12px")).toBe("-0.75rem");
  });
  it("keeps hairlines and non-px tokens", () => {
    expect(pxToRem("1px")).toBe("1px");
    expect(pxToRem("0 -1px")).toBe("0 -1px");
    expect(pxToRem("0.5px")).toBe("0.5px");
    expect(pxToRem("var(--gap-10px)")).toBe("var(--gap-10px)");
    expect(pxToRem("1.5rem")).toBe("1.5rem");
  });
});

describe("isScalableProp", () => {
  it("covers sizing, spacing, offsets, radius and type", () => {
    for (const p of ["font-size", "--tw-leading", "min-height", "max-width", "padding-inline", "margin-top",
      "inset-inline", "gap", "left", "border-top-left-radius", "scroll-margin-top"]) {
      expect(isScalableProp(p), p).toBe(true);
    }
  });
  it("leaves borders, shadows, transforms and blur alone", () => {
    for (const p of ["border", "border-width", "outline-width", "box-shadow", "--tw-shadow", "transform",
      "filter", "backdrop-filter", "--tw-translate-y"]) {
      expect(isScalableProp(p), p).toBe(false);
    }
  });
});

describe("scalablePx plugin", () => {
  it("rewrites arbitrary px utilities and hand-written px", () => {
    const out = run(".text-\\[10px\\]{font-size:10px}.h-\\[36px\\]{height:36px;line-height:36px}.x{scroll-margin-top:88px}");
    expect(out).toContain("font-size:0.625rem");
    expect(out).toContain("height:2.25rem;line-height:2.25rem");
    expect(out).toContain("scroll-margin-top:5.5rem");
  });
  it("keeps borders, shadows, hairlines and safe-area math in px", () => {
    const css = ".a{border:1px solid red;box-shadow:0 4px 20px #000;height:1px}" +
      ".b{padding-bottom:max(0px, calc(env(safe-area-inset-bottom, 0px) - 20px))}";
    expect(run(css)).toBe(css);
  });
  it("converts container query params but not media query params", () => {
    const out = run("@container composer (max-width: 440px){.p{gap:4px}}@media (min-width: 640px){.q{width:300px}}");
    expect(out).toContain("@container composer (max-width: 27.5rem)");
    expect(out).toContain("gap:0.25rem");
    expect(out).toContain("@media (min-width: 640px)");
    expect(out).toContain("width:18.75rem");
  });
  it("skips CSS from node_modules", () => {
    const css = ".xterm{font-size:14px}";
    expect(run(css, "/app/frontend/node_modules/@xterm/xterm/css/xterm.css")).toBe(css);
  });
});
