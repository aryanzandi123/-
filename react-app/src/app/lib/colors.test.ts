import { describe, it, expect } from "vitest";

import { classifyArrow, isReverseVerb, isForwardVerb, ARROW_COLORS } from "./colors";

describe("classifyArrow", () => {
  it("classifies positive verbs", () => {
    expect(classifyArrow("activates")).toBe("positive");
    expect(classifyArrow("phosphorylates")).toBe("positive");
    expect(classifyArrow("deubiquitinates")).toBe("positive");
  });

  it("classifies negative verbs", () => {
    expect(classifyArrow("inhibits")).toBe("negative");
    expect(classifyArrow("ubiquitinates")).toBe("negative");
    expect(classifyArrow("degrades")).toBe("negative");
  });

  it("classifies binding + regulatory", () => {
    expect(classifyArrow("binds")).toBe("binding");
    expect(classifyArrow("regulates")).toBe("regulatory");
  });

  it("treats is_*_by as reverse", () => {
    expect(classifyArrow("is_substrate_of")).toBe("reverse");
    expect(classifyArrow("is_phosphorylated_by")).toBe("reverse");
    expect(classifyArrow("is_ubiquitinated_by")).toBe("reverse");
  });

  it("falls back to neutral for unknown / null / empty", () => {
    expect(classifyArrow("teleports")).toBe("neutral");
    expect(classifyArrow(null)).toBe("neutral");
    expect(classifyArrow(undefined)).toBe("neutral");
    expect(classifyArrow("")).toBe("neutral");
  });

  it("is case-insensitive", () => {
    expect(classifyArrow("ACTIVATES")).toBe("positive");
    expect(classifyArrow("Inhibits")).toBe("negative");
  });
});

describe("isReverseVerb", () => {
  it("flags is_*_by patterns", () => {
    expect(isReverseVerb("is_substrate_of")).toBe(true);
    expect(isReverseVerb("is_activated_by")).toBe(true);
  });
  it("returns false for forward verbs", () => {
    expect(isReverseVerb("activates")).toBe(false);
    expect(isReverseVerb("inhibits")).toBe(false);
  });
});

describe("isForwardVerb", () => {
  it("returns true for known forward verbs", () => {
    expect(isForwardVerb("activates")).toBe(true);
    expect(isForwardVerb("inhibits")).toBe(true);
    expect(isForwardVerb("binds")).toBe(true);
    expect(isForwardVerb("regulates")).toBe(true);
  });
  it("returns false for reverse + unknown", () => {
    expect(isForwardVerb("is_substrate_of")).toBe(false);
    expect(isForwardVerb("teleports")).toBe(false);
    expect(isForwardVerb(null)).toBe(false);
  });
});

describe("ARROW_COLORS palette", () => {
  it("has a color for every kind", () => {
    expect(ARROW_COLORS.positive).toMatch(/^#[0-9a-f]{6}$/i);
    expect(ARROW_COLORS.negative).toMatch(/^#[0-9a-f]{6}$/i);
    expect(ARROW_COLORS.binding).toMatch(/^#[0-9a-f]{6}$/i);
    expect(ARROW_COLORS.regulatory).toMatch(/^#[0-9a-f]{6}$/i);
    expect(ARROW_COLORS.reverse).toMatch(/^#[0-9a-f]{6}$/i);
    expect(ARROW_COLORS.neutral).toMatch(/^#[0-9a-f]{6}$/i);
  });
});
