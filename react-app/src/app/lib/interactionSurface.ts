import type { Claim, Interaction, Snapshot } from "@/types/api";

function asClaims(value: unknown): Claim[] {
  return Array.isArray(value) ? (value as Claim[]) : [];
}

function normalizeClaimForCard(claim: Claim): Claim {
  const c = claim as Claim & {
    function_name?: unknown;
    mechanism?: unknown;
    biological_consequence?: unknown;
    pathway_name?: unknown;
  };
  const normalized: Claim = { ...claim };

  if (!normalized.function && typeof c.function_name === "string") {
    normalized.function = c.function_name;
  }
  if (!normalized.cellular_process && typeof c.mechanism === "string") {
    normalized.cellular_process = c.mechanism;
  }
  if (normalized.biological_consequences == null && c.biological_consequence != null) {
    normalized.biological_consequences = c.biological_consequence as Claim["biological_consequences"];
  }
  if (normalized.pathway == null && typeof c.pathway_name === "string") {
    normalized.pathway = c.pathway_name;
  }

  return normalized;
}

export function claimsForInteraction(interaction: Interaction): Claim[] {
  const functions = asClaims(interaction.functions);
  const claims = asClaims(interaction.claims);
  const selected =
    interaction._is_chain_link && functions.length > 0
      ? functions
      : claims.length > 0
        ? claims
        : functions;

  // Chain-hop rows are synthesized for a specific leg. The DB-backed `claims`
  // collection can contain broader pair claims, while `functions` is the
  // hop-specific card list the modal should render. Direct rows should prefer
  // DB claims because they carry the post-save arrow/direction/function_context
  // contract. Normalize both surfaces into the card shape used by FunctionCard.
  return selected.map(normalizeClaimForCard);
}

function sameSymbol(a: string | null | undefined, b: string): boolean {
  return (a ?? "").toUpperCase() === b;
}

function hasChain(interaction: Interaction, chainId: number): boolean {
  if (interaction.chain_id === chainId) return true;
  if (Array.isArray(interaction.chain_ids) && interaction.chain_ids.includes(chainId)) {
    return true;
  }
  return Boolean(
    Array.isArray(interaction.all_chains) &&
      interaction.all_chains.some((chain) => chain.chain_id === chainId),
  );
}

export function selectInteractionForEdge(
  snap: Pick<Snapshot, "interactions"> | null | undefined,
  source: string,
  target: string,
  chainId: number | null | undefined,
): Interaction | null {
  const list = Array.isArray(snap?.interactions) ? snap.interactions : [];
  const src = source.toUpperCase();
  const tgt = target.toUpperCase();
  const pairMatches = list.filter((interaction) => {
    const s = (interaction.source ?? "").toUpperCase();
    const t = (interaction.target ?? "").toUpperCase();
    return (s === src && t === tgt) || (s === tgt && t === src);
  });

  if (chainId != null) {
    const chainMatches = pairMatches.filter((interaction) => hasChain(interaction, chainId));
    const exactChainHop = chainMatches.find(
      (interaction) =>
        interaction._is_chain_link &&
        sameSymbol(interaction.source, src) &&
        sameSymbol(interaction.target, tgt),
    );
    if (exactChainHop) return exactChainHop;

    const anyChainHop = chainMatches.find((interaction) => interaction._is_chain_link);
    if (anyChainHop) return anyChainHop;

    const exactDirectional = chainMatches.find(
      (interaction) =>
        sameSymbol(interaction.source, src) && sameSymbol(interaction.target, tgt),
    );
    if (exactDirectional) return exactDirectional;

    if (chainMatches[0]) return chainMatches[0];
  }

  return pairMatches.find((interaction) => !interaction._is_chain_link) ?? pairMatches[0] ?? null;
}
