from typing import Dict, Any

from utils.interaction_contract import normalize_arrow


def aggregate_function_arrows(interactor: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aggregate function-level arrows into interaction-level arrows field.

    Computes:
    - `arrows`: Dict mapping direction → list of unique interaction_effect types
    - `arrow`: Backward-compat field (most common interaction_effect or 'regulates' if mixed)
    - `direction`: main_to_primary | primary_to_main (always asymmetric)

    DIRECTIONALITY RULES:
    - If a function has an invalid direction value, it is folded into
      main_to_primary (one vote for each canonical direction).
    - When functions exist in both directions with different names, the
      dominant direction wins; ties go to primary_to_main (conservative).
    - Ties default to primary_to_main (conservative: assume interactor acts on query).

    Args:
        interactor: Interactor dict with functions[] containing interaction_effect/interaction_direction fields

    Returns:
        Updated interactor dict with arrows and arrow fields
    """
    functions = interactor.get("functions", [])

    if not functions:
        interactor["arrow"] = "binds"
        interactor["arrows"] = {"main_to_primary": ["binds"]}
        interactor["direction"] = "main_to_primary"
        return interactor

    # Collect arrows by direction AND track function names per direction
    arrows_by_direction = {
        "main_to_primary": set(),
        "primary_to_main": set(),
    }

    # Track unique function names per direction (for mixed-direction detection)
    function_names_by_direction = {
        "main_to_primary": set(),
        "primary_to_main": set(),
    }

    direction_counts = {
        "main_to_primary": 0,
        "primary_to_main": 0,
    }

    for fn in functions:
        if not isinstance(fn, dict):
            continue

        interaction_effect = normalize_arrow(
            fn.get("interaction_effect", fn.get("arrow", "regulates"))
        )
        interaction_direction = fn.get("interaction_direction", fn.get("direction", ""))
        func_name = fn.get("function", "")

        # Handle invalid direction values: default to main_to_primary (don't double-count)
        if interaction_direction not in ("main_to_primary", "primary_to_main", ""):
            direction_counts["main_to_primary"] += 1
            arrows_by_direction["main_to_primary"].add(interaction_effect)
            function_names_by_direction["main_to_primary"].add(func_name)
        elif interaction_direction == "primary_to_main":
            direction_counts["primary_to_main"] += 1
            arrows_by_direction["primary_to_main"].add(interaction_effect)
            function_names_by_direction["primary_to_main"].add(func_name)
        else:
            # Default: main_to_primary (includes empty/missing direction)
            direction_counts["main_to_primary"] += 1
            arrows_by_direction["main_to_primary"].add(interaction_effect)
            function_names_by_direction["main_to_primary"].add(func_name)

    # Build arrows dict (remove empty directions)
    arrows = {
        k: sorted(list(v))
        for k, v in arrows_by_direction.items() if v
    }

    # Determine summary arrow field
    all_arrows = set()
    for arrow_list in arrows.values():
        all_arrows.update(arrow_list)

    if len(all_arrows) == 0:
        arrow = "binds"
    elif len(all_arrows) == 1:
        arrow = list(all_arrows)[0]
    else:
        arrow = "regulates"

    # Determine primary direction
    m2p_count = direction_counts["main_to_primary"]
    p2m_count = direction_counts["primary_to_main"]

    # When functions exist in both directions, pick the dominant one.
    # "bidirectional" is not a valid storage value (DB CHECK constraint blocks it).
    if p2m_count > m2p_count:
        direction = "primary_to_main"
    elif m2p_count > p2m_count:
        direction = "main_to_primary"
    elif p2m_count == m2p_count and p2m_count > 0:
        # Tie: default to primary_to_main (conservative - assume interactor acts on query)
        # This prevents upstream interactors from being mislabeled as downstream
        direction = "primary_to_main"
    else:
        direction = "main_to_primary"

    interactor["arrows"] = arrows
    interactor["arrow"] = arrow
    interactor["direction"] = direction

    return interactor
