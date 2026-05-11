-- One-off migration: bring existing per-device sensor tables up to spec.
-- 1. Add PRIMARY KEY (timestamp) for §8.2 idempotency.
-- 2. Convert to TimescaleDB hypertable for §5.2 query performance.
--
-- Tables with duplicate timestamps will fail step 1; the migration emits a NOTICE
-- and continues. Operators must dedupe manually before re-running for those tables.

DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND (
              table_name LIKE 'apollo_air_1_%'
              OR table_name LIKE 'apollo_msr_2_%'
              OR table_name LIKE 'athom_smart_plug_v2_%'
              OR table_name LIKE 'airgradient_one_%'
              OR table_name LIKE 'sensibo_%'
              OR table_name LIKE 'zigbee2mqtt_%'
          )
    LOOP
        -- Add primary key on timestamp.
        BEGIN
            EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (timestamp);', tbl);
            RAISE NOTICE 'Added PK to %', tbl;
        EXCEPTION
            WHEN invalid_table_definition THEN
                RAISE NOTICE 'Skipping % — PK already exists', tbl;
            WHEN unique_violation THEN
                RAISE NOTICE 'Skipping % — duplicate timestamps present (dedupe required)', tbl;
            WHEN OTHERS THEN
                RAISE NOTICE 'Skipping % — %', tbl, SQLERRM;
        END;

        -- Convert to TimescaleDB hypertable.
        BEGIN
            EXECUTE format(
                'SELECT create_hypertable(%L, %L, if_not_exists => TRUE);',
                tbl, 'timestamp'
            );
        EXCEPTION
            WHEN undefined_function THEN
                RAISE NOTICE 'TimescaleDB not installed; skipping hypertable for %', tbl;
            WHEN OTHERS THEN
                RAISE NOTICE 'create_hypertable failed for % — %', tbl, SQLERRM;
        END;
    END LOOP;
END
$$;
