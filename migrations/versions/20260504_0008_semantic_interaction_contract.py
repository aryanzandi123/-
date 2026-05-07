"""Tighten semantic arrow and claim-direction contracts.

Revision ID: 20260504_0008
Revises: 20260503_0007
Create Date: 2026-05-04

The live DB was structurally migrated, but several semantic fields still
drifted:

* claim directions could be NULL even though readers expect pair-local
  ``main_to_primary`` / ``primary_to_main`` values;
* ``complex`` leaked as a fifth arrow class, while the UI contract has four
  arrows and should render co-complex biology as ``binds``;
* scalar ``interactions.arrow``, JSONB ``interactions.arrows``, and
  ``interactions.data.arrow`` disagreed for legacy rows;
* chain hop JSON sometimes carried stale arrows copied from pre-canonical rows.

This migration backfills those rows, then adds checks so new writes cannot
reopen the same frontend/database mismatch.
"""
from __future__ import annotations

from alembic import op


revision = "20260504_0008"
down_revision = "20260503_0007"
branch_labels = None
depends_on = None


ARROW_CHECK = "arrow IS NULL OR arrow IN ('activates', 'inhibits', 'binds', 'regulates')"
CLAIM_DIRECTION_CHECK = "direction IN ('main_to_primary', 'primary_to_main')"
FUNCTION_CONTEXT_CHECK = "function_context IN ('direct', 'net', 'chain_derived', 'mixed')"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp._propaths_normalize_arrow(
            value text,
            default_arrow text
        )
        RETURNS text AS $$
            SELECT CASE
                WHEN value IS NULL OR btrim(value) = '' THEN default_arrow
                WHEN lower(btrim(value)) IN (
                    'activates', 'activate', 'activated', 'activation',
                    'promotes', 'enhances', 'stimulates', 'upregulates',
                    'increases'
                ) THEN 'activates'
                WHEN lower(btrim(value)) IN (
                    'inhibits', 'inhibit', 'inhibited', 'inhibition',
                    'suppresses', 'represses', 'blocks', 'downregulates',
                    'decreases', 'degrades'
                ) THEN 'inhibits'
                WHEN lower(btrim(value)) IN (
                    'binds', 'bind', 'binding', 'bound', 'interacts',
                    'associates', 'complex', 'complexes',
                    'complex formation', 'forms complex'
                ) THEN 'binds'
                WHEN lower(btrim(value)) IN (
                    'regulates', 'regulate', 'regulated', 'modulates',
                    'modulation', 'affects', 'unknown', 'unk', 'none', 'null'
                ) THEN 'regulates'
                ELSE default_arrow
            END
        $$ LANGUAGE SQL IMMUTABLE
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp._propaths_semantic_direction(
            value text,
            default_direction text
        )
        RETURNS text AS $$
            SELECT CASE
                WHEN lower(btrim(coalesce(value, ''))) = 'main_to_primary'
                    THEN 'main_to_primary'
                WHEN lower(btrim(coalesce(value, ''))) = 'primary_to_main'
                    THEN 'primary_to_main'
                WHEN lower(btrim(coalesce(value, ''))) = 'b_to_a'
                    THEN 'primary_to_main'
                WHEN lower(btrim(coalesce(value, ''))) = 'a_to_b'
                    THEN 'main_to_primary'
                WHEN default_direction IN ('main_to_primary', 'primary_to_main')
                    THEN default_direction
                ELSE 'main_to_primary'
            END
        $$ LANGUAGE SQL IMMUTABLE
        """
    )

    # Normalize scalar columns first.
    op.execute(
        """
        UPDATE interactions
        SET arrow = CASE
            WHEN arrow IS NULL OR btrim(arrow) = '' THEN 'binds'
            ELSE pg_temp._propaths_normalize_arrow(arrow, 'regulates')
        END
        WHERE arrow IS NULL
           OR btrim(arrow) = ''
           OR arrow <> pg_temp._propaths_normalize_arrow(arrow, 'regulates')
        """
    )
    op.execute(
        """
        UPDATE interaction_claims
        SET arrow = CASE
            WHEN arrow IS NULL OR btrim(arrow) = '' THEN 'regulates'
            ELSE pg_temp._propaths_normalize_arrow(arrow, 'regulates')
        END
        WHERE arrow IS NULL
           OR btrim(arrow) = ''
           OR arrow <> pg_temp._propaths_normalize_arrow(arrow, 'regulates')
        """
    )

    # Backfill and constrain contexts defensively. 0007 already removed NULLs,
    # but older local DBs may still carry invalid text.
    op.execute(
        """
        UPDATE interactions
        SET function_context = 'direct'
        WHERE function_context IS NULL
           OR function_context NOT IN ('direct', 'net', 'chain_derived', 'mixed')
        """
    )
    op.execute(
        """
        UPDATE interaction_claims
        SET function_context = 'direct'
        WHERE function_context IS NULL
           OR function_context NOT IN ('direct', 'net', 'chain_derived', 'mixed')
        """
    )

    # Claim directions are pair-local UI semantics, not absolute protein-id
    # directions. Prefer claim JSON if it exists, otherwise map the parent row.
    op.execute(
        """
        WITH mapped AS (
            SELECT
                c.id,
                pg_temp._propaths_semantic_direction(
                    coalesce(
                        c.raw_function_data->>'interaction_direction',
                        c.raw_function_data->>'direction',
                        c.raw_function_data->>'likely_direction',
                        c.direction
                    ),
                    pg_temp._propaths_semantic_direction(i.direction, 'main_to_primary')
                ) AS new_direction
            FROM interaction_claims c
            JOIN interactions i ON i.id = c.interaction_id
            WHERE c.direction IS NULL
               OR c.direction NOT IN ('main_to_primary', 'primary_to_main')
        )
        UPDATE interaction_claims c
        SET direction = mapped.new_direction
        FROM mapped
        WHERE c.id = mapped.id
        """
    )

    # Collapse the legacy scalar/JSONB arrow split to one canonical value.
    op.execute(
        """
        UPDATE interactions
        SET arrows = jsonb_build_object(
            CASE WHEN direction = 'b_to_a' THEN 'b_to_a' ELSE 'a_to_b' END,
            jsonb_build_array(arrow)
        )
        WHERE arrow IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE interactions
        SET data = jsonb_set(
            jsonb_set(
                jsonb_set(coalesce(data, '{}'::jsonb), '{arrow}', to_jsonb(arrow), true),
                '{arrows}', coalesce(arrows, '{}'::jsonb), true
            ),
            '{function_context}', to_jsonb(function_context), true
        )
        WHERE data IS NOT NULL
        """
    )

    # Normalize chain hop JSON in both canonical and legacy storage locations.
    op.execute(
        """
        WITH normalized AS (
            SELECT
                ic.id,
                jsonb_agg(
                    jsonb_set(
                        hop,
                        '{arrow}',
                        to_jsonb(pg_temp._propaths_normalize_arrow(hop->>'arrow', 'binds')),
                        true
                    )
                    ORDER BY ord
                ) AS chain_with_arrows
            FROM indirect_chains ic
            CROSS JOIN LATERAL jsonb_array_elements(ic.chain_with_arrows)
                WITH ORDINALITY AS e(hop, ord)
            WHERE jsonb_typeof(ic.chain_with_arrows) = 'array'
            GROUP BY ic.id
        )
        UPDATE indirect_chains ic
        SET chain_with_arrows = normalized.chain_with_arrows
        FROM normalized
        WHERE ic.id = normalized.id
        """
    )
    op.execute(
        """
        WITH normalized AS (
            SELECT
                i.id,
                jsonb_agg(
                    jsonb_set(
                        hop,
                        '{arrow}',
                        to_jsonb(pg_temp._propaths_normalize_arrow(hop->>'arrow', 'binds')),
                        true
                    )
                    ORDER BY ord
                ) AS chain_with_arrows
            FROM interactions i
            CROSS JOIN LATERAL jsonb_array_elements(i.chain_with_arrows)
                WITH ORDINALITY AS e(hop, ord)
            WHERE jsonb_typeof(i.chain_with_arrows) = 'array'
            GROUP BY i.id
        )
        UPDATE interactions i
        SET chain_with_arrows = normalized.chain_with_arrows
        FROM normalized
        WHERE i.id = normalized.id
        """
    )
    op.execute(
        """
        WITH normalized AS (
            SELECT
                i.id,
                jsonb_agg(
                    jsonb_set(
                        hop,
                        '{arrow}',
                        to_jsonb(pg_temp._propaths_normalize_arrow(hop->>'arrow', 'binds')),
                        true
                    )
                    ORDER BY ord
                ) AS chain_with_arrows
            FROM interactions i
            CROSS JOIN LATERAL jsonb_array_elements(i.data->'chain_with_arrows')
                WITH ORDINALITY AS e(hop, ord)
            WHERE jsonb_typeof(i.data->'chain_with_arrows') = 'array'
            GROUP BY i.id
        )
        UPDATE interactions i
        SET data = jsonb_set(coalesce(i.data, '{}'::jsonb), '{chain_with_arrows}', normalized.chain_with_arrows, true)
        FROM normalized
        WHERE i.id = normalized.id
        """
    )
    op.execute(
        """
        UPDATE interactions i
        SET
            chain_with_arrows = ic.chain_with_arrows,
            data = jsonb_set(coalesce(i.data, '{}'::jsonb), '{chain_with_arrows}', ic.chain_with_arrows, true)
        FROM indirect_chains ic
        WHERE ic.origin_interaction_id = i.id
          AND ic.chain_with_arrows IS NOT NULL
        """
    )

    # Finally enforce the repaired contract.
    op.execute("ALTER TABLE interactions DROP CONSTRAINT IF EXISTS valid_function_context")
    op.execute("ALTER TABLE interactions DROP CONSTRAINT IF EXISTS valid_interaction_arrow")
    op.create_check_constraint("valid_function_context", "interactions", FUNCTION_CONTEXT_CHECK)
    op.create_check_constraint("valid_interaction_arrow", "interactions", ARROW_CHECK)

    op.execute("ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS valid_claim_arrow")
    op.execute("ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS valid_claim_direction")
    op.execute("ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS valid_claim_function_context")
    op.execute("ALTER TABLE interaction_claims ALTER COLUMN direction SET DEFAULT 'main_to_primary'")
    op.execute("ALTER TABLE interaction_claims ALTER COLUMN direction SET NOT NULL")
    op.create_check_constraint("valid_claim_arrow", "interaction_claims", ARROW_CHECK)
    op.create_check_constraint("valid_claim_direction", "interaction_claims", CLAIM_DIRECTION_CHECK)
    op.create_check_constraint("valid_claim_function_context", "interaction_claims", FUNCTION_CONTEXT_CHECK)


def downgrade() -> None:
    op.execute("ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS valid_claim_function_context")
    op.execute("ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS valid_claim_direction")
    op.execute("ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS valid_claim_arrow")
    op.execute("ALTER TABLE interaction_claims ALTER COLUMN direction DROP NOT NULL")
    op.execute("ALTER TABLE interaction_claims ALTER COLUMN direction DROP DEFAULT")

    op.execute("ALTER TABLE interactions DROP CONSTRAINT IF EXISTS valid_interaction_arrow")
    op.execute("ALTER TABLE interactions DROP CONSTRAINT IF EXISTS valid_function_context")
    op.create_check_constraint(
        "valid_function_context",
        "interactions",
        "function_context IS NULL OR function_context IN ('direct', 'net', 'chain_derived', 'mixed')",
    )
