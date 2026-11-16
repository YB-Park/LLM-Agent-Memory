-- Keep the applied legacy migrations immutable. This conversion runs atomically with its
-- schema_migrations entry. Stop the old API, workers and admin processes before upgrading.
SELECT id FROM system_control WHERE id = 1 FOR UPDATE;
LOCK TABLE namespaces IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF (SELECT count(*) FROM namespaces) > 1 THEN
        RAISE EXCEPTION 'single-memory upgrade refused: multiple legacy namespaces (no automatic merge)';
    END IF;
    IF EXISTS (SELECT 1 FROM namespaces WHERE write_gate <> 'open' OR maintenance_generation_id IS NOT NULL)
        OR EXISTS (SELECT 1 FROM generations WHERE state = 'building') THEN
        RAISE EXCEPTION 'single-memory upgrade refused: finish or abort legacy maintenance first';
    END IF;
    IF EXISTS (SELECT 1 FROM namespaces n LEFT JOIN generations g ON g.id = n.active_generation_id
               WHERE g.state IS DISTINCT FROM 'servable')
        OR (SELECT count(*) FROM generations WHERE state = 'servable') > 1 THEN
        RAISE EXCEPTION 'single-memory upgrade refused: legacy database needs exactly one servable active generation';
    END IF;
END $$;

ALTER TABLE system_control
    ADD COLUMN active_generation_id uuid REFERENCES generations (id),
    ADD COLUMN maintenance_generation_id uuid REFERENCES generations (id),
    ADD COLUMN write_gate text NOT NULL DEFAULT 'open' CHECK (write_gate IN ('open', 'paused')),
    ADD COLUMN ingest_revision bigint NOT NULL DEFAULT 0,
    ADD CHECK ((write_gate = 'open') = (maintenance_generation_id IS NULL));
UPDATE system_control c SET active_generation_id = n.active_generation_id,
       ingest_revision = n.ingest_revision, policy_revision = c.policy_revision + 1
FROM namespaces n WHERE c.id = 1;

-- This historical mapping is only for unchanged v1 receipt HMACs and grant_reduce
-- ledger entries. It is never used to route requests or authorize access.
INSERT INTO audit_events (event_type, safe_details)
SELECT 'single_memory_migrated', jsonb_build_object('legacy_namespace_id', id) FROM namespaces;

ALTER TABLE principals
    ADD COLUMN can_read boolean NOT NULL DEFAULT false,
    ADD COLUMN can_write boolean NOT NULL DEFAULT false,
    ADD COLUMN can_manage boolean NOT NULL DEFAULT false,
    ADD CHECK (NOT can_manage OR can_write),
    ADD CHECK (NOT can_write OR can_read);
UPDATE principals p SET can_read = g.can_read, can_write = g.can_write, can_manage = g.can_manage
FROM namespace_grants g WHERE p.id = g.principal_id;

-- No attempt claimed by a stopped legacy worker may publish after conversion.
UPDATE jobs SET state = 'queued', attempt = attempt + 1, lease_owner = NULL,
       lease_until = NULL, not_before = now(), error_code = 'MIGRATION_FENCED'
WHERE state = 'running';

ALTER TABLE artifact_sources DROP COLUMN namespace_id;
ALTER TABLE artifacts DROP COLUMN namespace_id;
ALTER TABLE ingest_receipts DROP COLUMN namespace_id;
ALTER TABLE jobs DROP COLUMN namespace_id;
ALTER TABLE experiences DROP COLUMN namespace_id;
DROP TABLE namespace_grants;
ALTER TABLE generations DROP CONSTRAINT generations_namespace_id_fkey;
DROP TABLE namespaces;
ALTER TABLE generations DROP COLUMN namespace_id;

ALTER TABLE experiences ADD UNIQUE (id, source_version);
CREATE INDEX experiences_state_idx ON experiences (state, id);
ALTER TABLE jobs
    ADD UNIQUE (id, generation_id),
    ADD FOREIGN KEY (generation_id) REFERENCES generations (id),
    ADD FOREIGN KEY (experience_id, source_version) REFERENCES experiences (id, source_version);
ALTER TABLE ingest_receipts
    ADD PRIMARY KEY (principal_id, key_digest),
    ADD FOREIGN KEY (experience_id) REFERENCES experiences (id),
    ADD FOREIGN KEY (initial_job_id) REFERENCES jobs (id),
    ADD COLUMN request_version smallint NOT NULL DEFAULT 1 CHECK (request_version IN (1, 2));
ALTER TABLE ingest_receipts ALTER COLUMN request_version SET DEFAULT 2;
ALTER TABLE artifacts ADD FOREIGN KEY (job_id, generation_id) REFERENCES jobs (id, generation_id);
CREATE INDEX artifacts_gen_state_idx ON artifacts (generation_id, state);
ALTER TABLE artifact_sources
    ADD FOREIGN KEY (artifact_id) REFERENCES artifacts (id),
    ADD FOREIGN KEY (experience_id, source_version) REFERENCES experiences (id, source_version);

-- Old receipts can no longer authorize a generation switch. Preserve their IDs for inspection.
UPDATE generations SET comparison_receipt = comparison_receipt - 'namespace_id'
WHERE comparison_receipt IS NOT NULL;
CREATE UNIQUE INDEX generations_one_servable_idx ON generations (state) WHERE state = 'servable';
