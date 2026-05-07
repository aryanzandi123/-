import { describe, expect, it } from "vitest";

import {
  claimsForInteraction,
  selectInteractionForEdge,
  selectInteractionsForNode,
} from "./interactionSurface";
import type { Claim, Interaction, Snapshot } from "@/types/api";

const claim = (functionName: string, arrow = "binds"): Claim =>
  ({ function: functionName, arrow }) as Claim;

const interaction = (overrides: Partial<Interaction>): Interaction =>
  ({
    source: "TDP43",
    target: "XPO1",
    arrow: "binds",
    direction: "main_to_primary",
    type: "direct",
    interaction_type: "direct",
    depth: 1,
    ...overrides,
  }) as Interaction;

describe("claimsForInteraction", () => {
  it("uses hop-specific functions for chain links", () => {
    const inter = interaction({
      _is_chain_link: true,
      functions: [claim("Hop-specific exportin binding")],
      claims: [claim("Broader direct export"), claim("Hop-specific exportin binding")],
    });

    expect(claimsForInteraction(inter).map((c) => c.function)).toEqual([
      "Hop-specific exportin binding",
    ]);
  });

  it("still renders chain functions when DB claims are empty", () => {
    const inter = interaction({
      _is_chain_link: true,
      functions: [claim("Pair-specific chain biology")],
      claims: [],
    });

    expect(claimsForInteraction(inter)).toHaveLength(1);
  });

  it("prefers persisted claims for direct rows", () => {
    const inter = interaction({
      functions: [claim("Stale pre-save function", "activates")],
      claims: [
        {
          function_name: "Persisted DB claim",
          mechanism: "Persisted mechanism.",
          biological_consequence: ["Step 1"],
          pathway_name: "RNA Transport",
          arrow: "binds",
        } as Claim,
      ],
    });

    expect(claimsForInteraction(inter)).toMatchObject([
      {
        function: "Persisted DB claim",
        cellular_process: "Persisted mechanism.",
        biological_consequences: ["Step 1"],
        pathway: "RNA Transport",
        arrow: "binds",
      },
    ]);
  });
});

describe("selectInteractionForEdge", () => {
  it("selects the exact chain-hop row when a chain edge carries a chain id", () => {
    const direct = interaction({
      source: "XPO1",
      target: "TDP43",
      arrow: "inhibits",
      chain_id: 2619,
      all_chains: [
        {
          chain_id: 2619,
          role: "hop",
          chain_proteins: ["TDP43", "XPO1", "NXF1"],
          chain_with_arrows: [],
          pathway_name: "RNA Transport",
          discovered_in_query: "TDP43",
        },
      ],
    });
    const chainHop = interaction({
      source: "TDP43",
      target: "XPO1",
      arrow: "binds",
      _is_chain_link: true,
      chain_id: 2619,
    });
    const snap = { interactions: [direct, chainHop] } as Snapshot;

    expect(selectInteractionForEdge(snap, "TDP43", "XPO1", 2619)).toBe(chainHop);
  });

  it("selects the non-chain direct row for ordinary edge clicks", () => {
    const direct = interaction({ source: "XPO1", target: "TDP43", arrow: "inhibits" });
    const chainHop = interaction({
      source: "TDP43",
      target: "XPO1",
      arrow: "binds",
      _is_chain_link: true,
      chain_id: 2619,
    });
    const snap = { interactions: [chainHop, direct] } as Snapshot;

    expect(selectInteractionForEdge(snap, "TDP43", "XPO1", null)).toBe(direct);
  });

  it("uses hop index to disambiguate repeated chain edges with the same pair", () => {
    const firstHop = interaction({
      source: "TDP43",
      target: "EWSR1",
      _is_chain_link: true,
      chain_id: 2621,
      hop_index: 0,
      functions: [claim("First hop")],
    });
    const secondHop = interaction({
      source: "TDP43",
      target: "EWSR1",
      _is_chain_link: true,
      chain_id: 2621,
      hop_index: 2,
      functions: [claim("Second hop")],
    });
    const snap = { interactions: [firstHop, secondHop] } as Snapshot;

    expect(selectInteractionForEdge(snap, "TDP43", "EWSR1", 2621, 2)).toBe(secondHop);
  });
});

describe("selectInteractionsForNode", () => {
  it("scopes duplicate protein node modals to adjacent chain hops", () => {
    const inbound = interaction({
      source: "EIF2AK3",
      target: "EWSR1",
      _is_chain_link: true,
      chain_id: 2621,
      hop_index: 0,
    });
    const outbound = interaction({
      source: "EWSR1",
      target: "TDP43",
      _is_chain_link: true,
      chain_id: 2621,
      hop_index: 1,
    });
    const otherChain = interaction({
      source: "TDP43",
      target: "EWSR1",
      _is_chain_link: true,
      chain_id: 2622,
      hop_index: 0,
    });
    const direct = interaction({
      source: "EWSR1",
      target: "TDP43",
    });
    const snap = { interactions: [inbound, outbound, otherChain, direct] } as Snapshot;

    expect(
      selectInteractionsForNode(snap, "EWSR1", {
        chainId: 2621,
        chainPosition: 1,
      }),
    ).toEqual([inbound, outbound]);
  });
});
