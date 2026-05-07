import { describe, it, expect } from "vitest";

import { canonicalPairKey, normalizePathwayName, pathwayNameMatches } from "./normalize";

describe("canonicalPairKey", () => {
  it("is order-independent", () => {
    expect(canonicalPairKey("ATXN3", "HDAC6")).toBe(canonicalPairKey("HDAC6", "ATXN3"));
  });

  it("upper-cases + trims", () => {
    expect(canonicalPairKey(" atxn3 ", "Hdac6")).toBe("ATXN3|HDAC6");
  });

  it("uses pipe separator with alphabetical ordering", () => {
    expect(canonicalPairKey("Z", "A")).toBe("A|Z");
  });
});

describe("normalizePathwayName", () => {
  it("handles common separator drift", () => {
    expect(normalizePathwayName("Protein Quality Control")).toBe(
      normalizePathwayName("protein_quality_control"),
    );
    expect(normalizePathwayName("Protein  Quality  Control")).toBe(
      normalizePathwayName("Protein Quality Control"),
    );
  });

  it("preserves & and / and -", () => {
    expect(normalizePathwayName("PQC & Aggrephagy")).toBe("pqc & aggrephagy");
    expect(normalizePathwayName("CNX/CRT Cycle")).toBe("cnx/crt cycle");
    expect(normalizePathwayName("ER-Stress")).toBe("er-stress");
  });

  it("returns empty for null / undefined / empty", () => {
    expect(normalizePathwayName(null)).toBe("");
    expect(normalizePathwayName(undefined)).toBe("");
    expect(normalizePathwayName("")).toBe("");
  });

  it("strips other punctuation", () => {
    expect(normalizePathwayName("Foo, Bar!")).toBe("foo bar");
  });
});

describe("pathwayNameMatches", () => {
  it("returns true for trivial equality", () => {
    expect(pathwayNameMatches("Autophagy", "Autophagy")).toBe(true);
  });
  it("returns true across separator drift", () => {
    expect(pathwayNameMatches("Protein Quality Control", "protein_quality_control")).toBe(true);
  });
  it("returns false for distinct pathways", () => {
    expect(pathwayNameMatches("Autophagy", "Apoptosis")).toBe(false);
  });
  it("returns false for either side null/empty", () => {
    expect(pathwayNameMatches(null, "Autophagy")).toBe(false);
    expect(pathwayNameMatches("Autophagy", "")).toBe(false);
  });
});
