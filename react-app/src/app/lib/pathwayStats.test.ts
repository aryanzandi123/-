import { describe, it, expect } from "vitest";

import {
  derivePathwayStats,
  sortPathwayStats,
  claimPassScore,
} from "./pathwayStats";
import type { Snapshot, Claim } from "@/types/api";

const mkPathway = (
  id: string,
  name: string,
  overrides: Record<string, unknown> = {},
) => ({ id, name, hierarchy_level: 0, parent_ids: [], child_ids: [], ancestor_ids: [], interactor_ids: [], ...overrides });

describe("claimPassScore", () => {
  it("returns null when all signals are placeholder", () => {
    expect(claimPassScore({} as Claim)).toBeNull();
  });

  it("returns 1.0 for fully-passing claim (≥6 sentences, ≥3 cascades, ≥3 PMIDs)", () => {
    const c: Claim = {
      effect_description:
        "S1. S2. S3. S4. S5. S6. S7.",
      biological_consequences: ["a", "b", "c"],
      evidence: [{ pmid: "1" }, { pmid: "2" }, { pmid: "3" }],
    };
    expect(claimPassScore(c)).toBe(1);
  });

  it("returns null when all three signals are unmeasurable", () => {
    const c: Claim = {
      effect_description: "",
      biological_consequences: [],
      evidence: [],
    };
    expect(claimPassScore(c)).toBeNull();
  });

  it("returns 0 for a claim with one weak signal but failing thresholds", () => {
    const c: Claim = {
      effect_description: "One.",
      biological_consequences: [],
      evidence: [],
    };
    // sentenceCount("One.") splits on /[.!?]+\s+/ → ["One."] → 1 sentence
    // dPass = (1 < 3) → 0; cPass = 0; ePass = 0 → 0/3 = 0
    expect(claimPassScore(c)).toBe(0);
  });

  it("returns ~0.5 for half-passing claim", () => {
    const c: Claim = {
      effect_description: "S1. S2. S3. S4.",
      biological_consequences: ["a", "b"],
      evidence: [{ pmid: "1" }],
    };
    // depth=0.5, cascade=0.5, evidence=0.5 → 0.5
    expect(claimPassScore(c)).toBe(0.5);
  });
});

describe("derivePathwayStats", () => {
  it("returns empty map for null snap", () => {
    expect(derivePathwayStats(null).size).toBe(0);
  });

  it("counts direct + chain interactions per pathway", () => {
    const snap: Snapshot = {
      main: "ATXN3",
      pathways: [
        mkPathway("p1", "Autophagy", { interactor_ids: ["HDAC6"] }),
      ],
      interactions: [
        // direct
        {
          source: "ATXN3",
          target: "HDAC6",
          arrow: "activates",
          direction: "main_to_primary",
          type: "direct",
          interaction_type: "direct",
          depth: 1,
          pathways: ["Autophagy"],
        } as never,
        // chain
        {
          source: "ATXN3",
          target: "SQSTM1",
          arrow: "activates",
          direction: "main_to_primary",
          type: "indirect",
          interaction_type: "indirect",
          depth: 2,
          _is_chain_link: true,
          chain_id: 1,
          all_chains: [
            {
              chain_id: 1,
              role: "origin",
              chain_proteins: ["ATXN3", "HDAC6", "SQSTM1"],
              chain_with_arrows: [],
              pathway_name: "Autophagy",
              discovered_in_query: "ATXN3",
            },
          ],
        } as never,
      ],
    };

    const stats = derivePathwayStats(snap);
    const auto = stats.get("p1")!;
    expect(auto.directCount).toBe(1);
    expect(auto.chainCount).toBe(1);
    expect(auto.memberProteins.has("HDAC6")).toBe(true);
    expect(auto.memberProteins.has("SQSTM1")).toBe(true);
    expect(auto.letterGrade).toBe("—"); // no quality data
  });

  it("respects pseudoTouching when interaction has pseudo flag", () => {
    const snap: Snapshot = {
      main: "ATXN3",
      pathways: [mkPathway("p1", "X")],
      interactions: [
        {
          source: "ATXN3",
          target: "RNA",
          arrow: "binds",
          direction: "main_to_primary",
          type: "direct",
          interaction_type: "direct",
          depth: 1,
          pathways: ["X"],
          _target_is_pseudo: true,
        } as never,
      ],
    };
    const s = derivePathwayStats(snap).get("p1")!;
    expect(s.pseudoTouching).toBe(true);
  });

  it("isCatchAll flags depth-0 pathways that swallow ≥80% of interactions", () => {
    // Build a synthetic where one pathway has all interactions at depth 0
    const snap: Snapshot = {
      main: "ATXN3",
      pathways: [mkPathway("root", "Biological Process", { hierarchy_level: 0 })],
      interactions: Array.from({ length: 5 }, (_v, i) => ({
        source: "ATXN3",
        target: `P${i}`,
        arrow: "activates",
        direction: "main_to_primary",
        type: "direct",
        interaction_type: "direct",
        depth: 1,
        pathways: ["Biological Process"],
      })) as never[],
    };
    const s = derivePathwayStats(snap).get("root")!;
    expect(s.isCatchAll).toBe(true);
  });
});

describe("sortPathwayStats", () => {
  const stats = [
    { name: "A", depth: 0, directCount: 1, chainCount: 0, driftCorrected: 0, driftReportOnly: 0, passRateMean: 0.9, chainCount2: 0 },
    { name: "B", depth: 1, directCount: 5, chainCount: 1, driftCorrected: 1, driftReportOnly: 0, passRateMean: 0.6, chainCount2: 1 },
    { name: "C", depth: 2, directCount: 0, chainCount: 5, driftCorrected: 0, driftReportOnly: 2, passRateMean: null, chainCount2: 5 },
  ] as never[];

  it("relevance: highest interactor count first", () => {
    const sorted = sortPathwayStats(stats, "relevance");
    expect(sorted[0].name).toBe("B"); // 5+1 = 6
    expect(sorted[1].name).toBe("C"); // 0+5 = 5
  });

  it("alphabetical: A → Z", () => {
    const sorted = sortPathwayStats(stats, "alphabetical");
    expect(sorted.map((s) => s.name)).toEqual(["A", "B", "C"]);
  });

  it("hierarchy: shallower first", () => {
    const sorted = sortPathwayStats(stats, "hierarchy");
    expect(sorted[0].depth).toBe(0);
  });

  it("drift: most drift first", () => {
    const sorted = sortPathwayStats(stats, "drift");
    expect(sorted[0].name).toBe("C"); // 0 corrected + 2 report = 2
  });

  it("lowestPass: lower pass rate first; null last", () => {
    const sorted = sortPathwayStats(stats, "lowestPass");
    expect(sorted[0].name).toBe("B"); // 0.6
    expect(sorted[1].name).toBe("A"); // 0.9
    expect(sorted[2].name).toBe("C"); // null
  });

  it("mostChains: highest chainCount first", () => {
    const sorted = sortPathwayStats(stats, "mostChains");
    expect(sorted[0].name).toBe("C"); // 5
  });
});
