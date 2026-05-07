-- Migration script for schema changes (run once against existing database)
-- Safe to re-run: all statements use IF NOT EXISTS / IF EXISTS checks

-- G2: Add composite index for fast protein pair lookups
CREATE INDEX IF NOT EXISTS idx_interaction_pair_lookup
    ON interactions (protein_a_id, protein_b_id);

-- G4: Add CHECK constraints for enum validation
-- (PostgreSQL doesn't support IF NOT EXISTS for constraints, so use DO block)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'valid_function_context'
    ) THEN
        ALTER TABLE interactions
        ADD CONSTRAINT valid_function_context
        CHECK (function_context IS NULL OR function_context IN ('direct', 'net', 'chain_derived', 'mixed'));
    END IF;
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'Some rows violate valid_function_context — fixing...';
    UPDATE interactions SET function_context = NULL
    WHERE function_context IS NOT NULL
      AND function_context NOT IN ('direct', 'net', 'chain_derived', 'mixed');
    ALTER TABLE interactions
    ADD CONSTRAINT valid_function_context
    CHECK (function_context IS NULL OR function_context IN ('direct', 'net', 'chain_derived', 'mixed'));
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'valid_interaction_type'
    ) THEN
        ALTER TABLE interactions
        ADD CONSTRAINT valid_interaction_type
        CHECK (interaction_type IS NULL OR interaction_type IN ('direct', 'indirect'));
    END IF;
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'Some rows violate valid_interaction_type — fixing...';
    UPDATE interactions SET interaction_type = 'direct'
    WHERE interaction_type IS NOT NULL
      AND interaction_type NOT IN ('direct', 'indirect');
    ALTER TABLE interactions
    ADD CONSTRAINT valid_interaction_type
    CHECK (interaction_type IS NULL OR interaction_type IN ('direct', 'indirect'));
END $$;

-- Verify
SELECT
    (SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_interaction_pair_lookup') AS pair_index_exists,
    (SELECT count(*) FROM pg_constraint WHERE conname = 'valid_function_context') AS function_context_check,
    (SELECT count(*) FROM pg_constraint WHERE conname = 'valid_interaction_type') AS interaction_type_check;
