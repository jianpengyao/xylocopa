import { describe, it, expect } from "vitest";
import { focusTargetId, isToolRow } from "./chatFocus";

const text = (id) => ({ id, role: "AGENT", kind: "text" });
const tool = (id) => ({ id, role: "AGENT", kind: "tool_use" });
const activity = (id) => ({ id, role: "AGENT", kind: "tool_activity" });
const user = (id) => ({ id, role: "USER", kind: null });
const hook = (id) => ({ id, role: "SYSTEM", kind: "stop_hook" });

describe("focusTargetId", () => {
  const msgs = [
    user("u1"),
    tool("t1"), tool("t2"), activity("a1"), hook("h1"), tool("t3"),
    text("x1"),
    tool("t4"),
  ];

  it("maps a later tool row to its run leader", () => {
    expect(focusTargetId(msgs, "t2")).toBe("t1");
    expect(focusTargetId(msgs, "a1")).toBe("t1");
    // stop_hook rows are dropped before grouping, so they don't split a run
    expect(focusTargetId(msgs, "t3")).toBe("t1");
  });

  it("leaves leaders, non-tool rows and unknown ids alone", () => {
    expect(focusTargetId(msgs, "t1")).toBe("t1");
    expect(focusTargetId(msgs, "t4")).toBe("t4");
    expect(focusTargetId(msgs, "x1")).toBe("x1");
    expect(focusTargetId(msgs, "u1")).toBe("u1");
    expect(focusTargetId(msgs, "nope")).toBe("nope");
    expect(focusTargetId(null, "t2")).toBe("t2");
  });

  it("only AGENT tool_use rows count as tool rows", () => {
    expect(isToolRow({ role: "USER", kind: "tool_use" })).toBe(false);
    expect(isToolRow(activity("a"))).toBe(true);
    expect(isToolRow(text("x"))).toBe(false);
  });
});
