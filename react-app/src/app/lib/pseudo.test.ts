import { describe, it, expect } from "vitest";

import { isPseudoProtein, PSEUDO_NAMES } from "./pseudo";

describe("isPseudoProtein", () => {
  it("catches RNA family pseudo entries", () => {
    expect(isPseudoProtein("RNA")).toBe(true);
    expect(isPseudoProtein("mRNA")).toBe(true);
    expect(isPseudoProtein("tRNA")).toBe(true);
  });

  it("catches Ubiquitin family", () => {
    expect(isPseudoProtein("Ubiquitin")).toBe(true);
    expect(isPseudoProtein("K48-Ub")).toBe(true);
  });

  it("catches macromolecular complexes", () => {
    expect(isPseudoProtein("Proteasome")).toBe(true);
    expect(isPseudoProtein("Ribosome")).toBe(true);
  });

  it("returns false for real proteins", () => {
    expect(isPseudoProtein("ATXN3")).toBe(false);
    expect(isPseudoProtein("HDAC6")).toBe(false);
    expect(isPseudoProtein("STUB1")).toBe(false);
  });

  it("handles null / empty / whitespace", () => {
    expect(isPseudoProtein(null)).toBe(false);
    expect(isPseudoProtein(undefined)).toBe(false);
    expect(isPseudoProtein("")).toBe(false);
    expect(isPseudoProtein("   ")).toBe(false);
  });

  it("PSEUDO_NAMES is non-empty + all upper-case", () => {
    expect(PSEUDO_NAMES.size).toBeGreaterThan(10);
    for (const n of PSEUDO_NAMES) {
      expect(n).toBe(n.toUpperCase());
    }
  });
});
