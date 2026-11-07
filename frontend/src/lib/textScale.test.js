import { describe, it, expect, afterEach, vi } from "vitest";
import {
  TEXT_SCALE_EVENT, clampTextScale, designPx, effectiveTextScale,
  getMaxTextScale, getTextScale, rem, setTextScale,
} from "./textScale";

const root = () => document.documentElement;

afterEach(() => {
  localStorage.clear();
  root().style.removeProperty("--xy-text-scale");
  root().style.fontSize = "";
});

describe("clampTextScale", () => {
  it("snaps to the 5% grid without float noise", () => {
    expect(clampTextScale(1.15)).toBe(1.15);
    expect(clampTextScale(1.234)).toBe(1.25);
    expect(clampTextScale(0.9 + 0.05)).toBe(0.95);
  });
  it("keeps to the 85%-150% range and falls back to 1", () => {
    expect(clampTextScale(0.5)).toBe(0.85);
    expect(clampTextScale(3)).toBe(1.5);
    expect(clampTextScale(NaN)).toBe(1);
    expect(clampTextScale("1.1")).toBe(1.1);
  });
});

describe("getTextScale", () => {
  it("defaults to 1 and reads the stored value", () => {
    expect(getTextScale()).toBe(1);
    localStorage.setItem("xy:text-scale", "1.2");
    expect(getTextScale()).toBe(1.2);
  });
  it("sanitizes a bad stored value", () => {
    localStorage.setItem("xy:text-scale", "abc");
    expect(getTextScale()).toBe(1);
    localStorage.setItem("xy:text-scale", "9");
    expect(getTextScale()).toBe(1.5);
  });
});

describe("getMaxTextScale", () => {
  it("keeps 320 design px across the screen's short edge", () => {
    expect(getMaxTextScale({ width: 393, height: 852 })).toBe(1.2); // iPhone 15/16
    expect(getMaxTextScale({ width: 402, height: 874 })).toBe(1.25); // iPhone 16 Pro
    expect(getMaxTextScale({ width: 852, height: 393 })).toBe(1.2); // landscape-reporting screen
    expect(getMaxTextScale({ width: 744, height: 1133 })).toBe(1.5); // iPad mini
    expect(getMaxTextScale({ width: 320, height: 568 })).toBe(1);
  });
  it("does not cap when the screen size is unknown", () => {
    expect(getMaxTextScale({ width: 0, height: 0 })).toBe(1.5);
    expect(getMaxTextScale(null)).toBe(1.5);
  });
});

describe("effectiveTextScale", () => {
  it("caps a scale above 1 by the window width", () => {
    expect(effectiveTextScale(1.2, 393)).toBe(1.2);
    expect(effectiveTextScale(1.3, 390)).toBe(1.219);
    expect(effectiveTextScale(1.5, 300)).toBe(1);
  });
  it("leaves a scale at or below 1 alone", () => {
    expect(effectiveTextScale(0.9, 300)).toBe(0.9);
    expect(effectiveTextScale(1, 200)).toBe(1);
  });
});

describe("setTextScale", () => {
  it("stores the scale, sets the root variable and notifies", () => {
    const onChange = vi.fn();
    window.addEventListener(TEXT_SCALE_EVENT, onChange);
    setTextScale(1.2);
    window.removeEventListener(TEXT_SCALE_EVENT, onChange);
    expect(localStorage.getItem("xy:text-scale")).toBe("1.2");
    expect(root().style.getPropertyValue("--xy-text-scale")).toBe("1.2");
    expect(onChange).toHaveBeenCalledTimes(1);
  });
  it("clears both at the default scale", () => {
    setTextScale(1.2);
    setTextScale(1);
    expect(localStorage.getItem("xy:text-scale")).toBeNull();
    expect(root().style.getPropertyValue("--xy-text-scale")).toBe("");
  });
});

describe("designPx / rem", () => {
  it("scales a design px value by the root font-size", () => {
    root().style.fontSize = "20px";
    expect(designPx(16)).toBe(20);
    expect(designPx(280)).toBe(350);
  });
  it("returns the value unchanged when the root size is unknown", () => {
    expect(designPx(160)).toBe(160);
  });
  it("formats design px as rem", () => {
    expect(rem(18)).toBe("1.125rem");
  });
});
