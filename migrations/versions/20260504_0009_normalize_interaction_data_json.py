"""Normalize arrow text inside stored interaction.data JSON.

Revision ID: 20260504_0009
Revises: 20260504_0008
Create Date: 2026-05-04

Revision 0008 repaired canonical columns and chain-hop JSON. This follow-up
cleans the remaining raw JSON payload copies, including nested
``functions`` and ``chain_link_functions`` entries, so old ``complex`` arrow
text does not survive in stored source blobs.
"""
from __future__ import annotations

from alembic import op


revision = "20260504_0009"
down_revision = "20260504_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp._propaths_normalize_json_arrow(value text)
        RETURNS text AS $$
            SELECT CASE lower(btrim(coalesce(value, '')))
                WHEN 'complex' THEN 'binds'
                WHEN 'complexes' THEN 'binds'
                WHEN 'complex formation' THEN 'binds'
                WHEN 'forms complex' THEN 'binds'
                WHEN 'modulates' THEN 'regulates'
                WHEN 'modulation' THEN 'regulates'
                ELSE value
            END
        $$ LANGUAGE SQL IMMUTABLE
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp._propaths_normalize_interaction_jsonb(value jsonb)
        RETURNS jsonb AS $$
        DECLARE
            key text;
            val jsonb;
            result jsonb;
        BEGIN
            IF value IS NULL THEN
                RETURN value;
            END IF;

            IF jsonb_typeof(value) = 'object' THEN
                result := '{}'::jsonb;
                FOR key, val IN SELECT * FROM jsonb_each(value) LOOP
                    IF key IN ('arrow', 'interaction_effect') AND jsonb_typeof(val) = 'string' THEN
                        result := result || jsonb_build_object(
                            key,
                            pg_temp._propaths_normalize_json_arrow(val #>> '{}')
                        );
                    ELSE
                        result := result || jsonb_build_object(
                            key,
                            pg_temp._propaths_normalize_interaction_jsonb(val)
                        );
                    END IF;
                END LOOP;
                RETURN result;
            END IF;

            IF jsonb_typeof(value) = 'array' THEN
                SELECT coalesce(
                    jsonb_agg(pg_temp._propaths_normalize_interaction_jsonb(elem) ORDER BY ord),
                    '[]'::jsonb
                )
                INTO result
                FROM jsonb_array_elements(value) WITH ORDINALITY AS e(elem, ord);
                RETURN result;
            END IF;

            RETURN value;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        UPDATE interactions
        SET data = pg_temp._propaths_normalize_interaction_jsonb(data)
        WHERE data IS NOT NULL
          AND (
              data::text LIKE '%"arrow": "complex"%'
              OR data::text LIKE '%"arrow": "complex formation"%'
              OR data::text LIKE '%"arrow": "forms complex"%'
              OR data::text LIKE '%"arrow": "modulates"%'
              OR data::text LIKE '%"interaction_effect": "complex"%'
              OR data::text LIKE '%"interaction_effect": "complex formation"%'
              OR data::text LIKE '%"interaction_effect": "forms complex"%'
              OR data::text LIKE '%"interaction_effect": "modulates"%'
          )
        """
    )


def downgrade() -> None:
    # Data normalization is not reversible without preserving a shadow copy of
    # the old JSON. Downgrading 0009 intentionally leaves cleaned values.
    pass
