import { describe, it, expect } from "vitest";

import {
  isPlaceholderText,
  isPathwayInContext,
  classifyClaim,
  mentionedEndpoints,
  pickEvidence,
  pickStringList,
  PLACEHOLDER_SNIPPETS,
} from "./claims";
import type { Claim } from "@/types/api";

describe("isPlaceholderText", () => {
  it("flags every documented placeholder fragment", () => {
    for (const frag of PLACEHOLDER_SNIPPETS) {
      expect(isPlaceholderText(frag)).toBe(true);
      expect(isPlaceholderText(`Some prefix ${frag} suffix`)).toBe(true);
    }
  });

  it("returns true for empty / null / non-string", () => {
    expect(isPlaceholderText("")).toBe(true);
    expect(isPlaceholderText(null)).toBe(true);
    expect(isPlaceholderText(undefined)).toBe(true);
    expect(isPlaceholderText(42 as unknown as string)).toBe(true);
  });

  it("returns false for substantive prose", () => {
    expect(isPlaceholderText("ATXN3 deubiquitinates HDAC6")).toBe(false);
  });

  it("is case-insensitive", () => {
    expect(isPlaceholderText("DATA NOT GENERATED")).toBe(true);
  });
});

describe("isPathwayInContext", () => {
  const claim = (overrides: Partial<Claim>): Claim =>
    ({ pathway: null, ...overrides }) as Claim;

  it("matches own pathway with separator drift", () => {
    expect(
      isPathwayInContext(claim({ pathway: "protein_quality_control" }), "Protein Quality Control"),
    ).toBe(true);
  });

  it("matches via _hierarchy ancestor walk", () => {
    expect(
      isPathwayInContext(
        claim({ pathway: "Aggrephagy", _hierarchy: ["Autophagy", "Cellular Catabolism"] }),
        "Autophagy",
      ),
    ).toBe(true);
  });

  it("returns false when nothing matches", () => {
    expect(isPathwayInContext(claim({ pathway: "Apoptosis" }), "Autophagy")).toBe(false);
  });

  it("returns false on empty context", () => {
    expect(isPathwayInContext(claim({ pathway: "Apoptosis" }), null)).toBe(false);
    expect(isPathwayInContext(claim({ pathway: "Apoptosis" }), "")).toBe(false);
  });

  it("handles object-shaped pathway field", () => {
    expect(
      isPathwayInContext(
        claim({ pathway: { canonical_name: "Autophagy" } as unknown as string }),
        "Autophagy",
      ),
    ).toBe(true);
  });
});

describe("classifyClaim", () => {
  it("flags _synthetic", () => {
    expect(classifyClaim({ _synthetic: true, pathway: "X" } as Claim)).toEqual({
      kind: "synthetic",
      pathway: "X",
    });
  });
  it("flags _thin_claim", () => {
    expect(
      classifyClaim({ _thin_claim: true, function: "Foo", cellular_process: "p" } as Claim),
    ).toMatchObject({ kind: "thin", title: "Foo" });
  });
  it("flags _synthetic_from_router", () => {
    expect(
      classifyClaim({ _synthetic_from_router: true, function: "F" } as Claim),
    ).toMatchObject({ kind: "router" });
  });
  it("treats __fallback__ as garbage", () => {
    expect(classifyClaim({ function: "__fallback__" } as Claim)).toMatchObject({
      kind: "garbage",
    });
  });
  it("returns normal for real function names", () => {
    expect(classifyClaim({ function: "ATXN3 Deubiquitination" } as Claim)).toEqual({
      kind: "normal",
      functionName: "ATXN3 Deubiquitination",
    });
  });
});

describe("pickEvidence", () => {
  it("filters out empty entries", () => {
    expect(pickEvidence([{}, { pmid: "12345" }, { quote: "Found X" }])).toHaveLength(2);
  });
  it("returns [] for non-array", () => {
    expect(pickEvidence(null)).toEqual([]);
    expect(pickEvidence("not an array" as unknown)).toEqual([]);
  });
});

describe("pickStringList", () => {
  it("returns array with placeholder filtered", () => {
    expect(pickStringList(["A", "data not generated", "B"])).toEqual(["A", "B"]);
  });
  it("handles single string", () => {
    expect(pickStringList("Hello")).toEqual(["Hello"]);
    expect(pickStringList("data not generated")).toEqual([]);
  });
  it("returns [] for null/undefined", () => {
    expect(pickStringList(null)).toEqual([]);
    expect(pickStringList(undefined)).toEqual([]);
  });
});

describe("mentionedEndpoints", () => {
  it("matches whole-word, case-insensitive gene symbols", () => {
    const hits = mentionedEndpoints(
      "CANX recruits PERK to the MAM during ER stress.",
      ["CANX", "PERK"],
    );
    expect(hits.has("CANX")).toBe(true);
    expect(hits.has("PERK")).toBe(true);
  });

  it("does not match substring inside larger token", () => {
    // "PERK" must not match "PERKINS" or "ATPERK"
    const hits = mentionedEndpoints("ATPERKAGE PERKINS upregulated", ["PERK"]);
    expect(hits.has("PERK")).toBe(false);
  });

  it("returns empty set when neither endpoint is mentioned", () => {
    const hits = mentionedEndpoints("HSPA9 stabilizes the membrane", ["CANX", "PERK"]);
    expect(hits.size).toBe(0);
  });

  it("tolerates hyphenated mentions like CANX-deficient", () => {
    const hits = mentionedEndpoints("CANX-deficient cells lacked the contact.", ["CANX"]);
    expect(hits.has("CANX")).toBe(true);
  });

  it("returns empty set for empty inputs", () => {
    expect(mentionedEndpoints("", ["CANX"]).size).toBe(0);
    expect(mentionedEndpoints("text", []).size).toBe(0);
  });
});
