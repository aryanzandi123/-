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

export function interactionHasChain(interaction: Interaction, chainId: number): boolean {
  if (interaction.chain_id === chainId) return true;
  if (Array.isArray(interaction.chain_ids) && interaction.chain_ids.includes(chainId)) {
    return true;
  }
  return Boolean(
    Array.isArray(interaction.all_chains) &&
      interaction.all_chains.some((chain) => chain.chain_id === chainId),
  );
}

export function interactionHopIndex(interaction: Interaction): number | null {
  const raw = interaction.hop_index ?? interaction._chain_position;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

export function edgeHopIndexFromChainPosition(chainPosition: number | null | undefined): number | null {
  return typeof chainPosition === "number" && Number.isFinite(chainPosition) && chainPosition > 0
    ? chainPosition - 1
    : null;
}

export function interactionTouchesProtein(interaction: Interaction, protein: string): boolean {
  const target = protein.toUpperCase();
  return sameSymbol(interaction.source, target) || sameSymbol(interaction.target, target);
}

export function selectInteractionsForNode(
  snap: Pick<Snapshot, "interactions"> | null | undefined,
  protein: string,
  options: { chainId?: number | null; chainPosition?: number | null } = {},
): Interaction[] {
  const list = Array.isArray(snap?.interactions) ? snap.interactions : [];
  const baseMatches = list.filter((interaction) => interactionTouchesProtein(interaction, protein));
  const chainId = options.chainId;
  if (chainId == null) return baseMatches;

  const chainMatches = baseMatches.filter((interaction) => interactionHasChain(interaction, chainId));
  const edgeBefore = edgeHopIndexFromChainPosition(options.chainPosition ?? null);
  const edgeAfter =
    typeof options.chainPosition === "number" && Number.isFinite(options.chainPosition)
      ? options.chainPosition
      : null;
  const hopCandidates = new Set<number>();
  if (edgeBefore != null) hopCandidates.add(edgeBefore);
  if (edgeAfter != null) hopCandidates.add(edgeAfter);
  if (hopCandidates.size === 0) return chainMatches;

  const scoped = chainMatches.filter((interaction) => {
    const hop = interactionHopIndex(interaction);
    return hop != null && hopCandidates.has(hop);
  });
  return scoped.length > 0 ? scoped : chainMatches;
}

export function selectInteractionForEdge(
  snap: Pick<Snapshot, "interactions"> | null | undefined,
  source: string,
  target: string,
  chainId: number | null | undefined,
  hopIndex: number | null | undefined = null,
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
    const chainMatches = pairMatches.filter((interaction) => interactionHasChain(interaction, chainId));
    const hopMatches =
      hopIndex == null
        ? chainMatches
        : chainMatches.filter((interaction) => interactionHopIndex(interaction) === hopIndex);
    const scopedMatches = hopMatches.length > 0 ? hopMatches : chainMatches;

    const exactChainHop = chainMatches.find(
      (interaction) =>
        interaction._is_chain_link &&
        (hopIndex == null || interactionHopIndex(interaction) === hopIndex) &&
        sameSymbol(interaction.source, src) &&
        sameSymbol(interaction.target, tgt),
    );
    if (exactChainHop) return exactChainHop;

    const anyChainHop = scopedMatches.find((interaction) => interaction._is_chain_link);
    if (anyChainHop) return anyChainHop;

    const exactDirectional = scopedMatches.find(
      (interaction) =>
        sameSymbol(interaction.source, src) && sameSymbol(interaction.target, tgt),
    );
    if (exactDirectional) return exactDirectional;

    if (scopedMatches[0]) return scopedMatches[0];
  }

  return pairMatches.find((interaction) => !interaction._is_chain_link) ?? pairMatches[0] ?? null;
}
