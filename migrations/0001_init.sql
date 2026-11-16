-- Phase 0 schema (docs/design/phase0-detailed-design-v0.1.md §4)

CREATE TABLE system_control (
    id              integer PRIMARY KEY CHECK (id = 1),
    policy_revision bigint  NOT NULL DEFAULT 0
);
INSERT INTO system_control (id) VALUES (1);

CREATE TABLE principals (
    id                uuid PRIMARY KEY,
    name              text NOT NULL,
    credential_digest text NOT NULL UNIQUE,
    kind              text NOT NULL CHECK (kind IN ('device', 'admin')),
    auth_epoch        bigint NOT NULL DEFAULT 1,
    revoked_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE namespaces (
    id                        uuid PRIMARY KEY,
    name                      text NOT NULL UNIQUE,
    active_generation_id      uuid,
    write_gate                text NOT NULL DEFAULT 'open' CHECK (write_gate IN ('open', 'paused')),
    maintenance_generation_id uuid,
    ingest_revision           bigint NOT NULL DEFAULT 0,
    created_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE namespace_grants (
    principal_id uuid NOT NULL REFERENCES principals (id),
    namespace_id uuid NOT NULL REFERENCES namespaces (id),
    can_read     boolean NOT NULL DEFAULT false,
    can_write    boolean NOT NULL DEFAULT false,
    can_manage   boolean NOT NULL DEFAULT false,
    PRIMARY KEY (principal_id, namespace_id),
    CHECK (NOT can_manage OR can_write),
    CHECK (NOT can_write OR can_read)
);

CREATE TABLE generations (
    id                     uuid PRIMARY KEY,
    namespace_id           uuid NOT NULL REFERENCES namespaces (id),
    base_generation_id     uuid,
    engine_spec            jsonb NOT NULL,
    spec_digest            text NOT NULL,
    index_profile          text NOT NULL CHECK (index_profile IN ('reference-keywords/v1', 'literal-substring/v1')),
    state                  text NOT NULL CHECK (state IN ('building', 'servable', 'retired', 'aborted')),
    snapshot_ingest_revision bigint,
    manifest_digest        text,
    result_revision        bigint NOT NULL DEFAULT 0,
    created_at             timestamptz NOT NULL DEFAULT now(),
    retired_ingest_revision bigint,
    comparison_receipt     jsonb,
    UNIQUE (id, namespace_id)
);

-- Circular pointers are created after generations; checked at commit.
ALTER TABLE namespaces
    ADD FOREIGN KEY (active_generation_id, id) REFERENCES generations (id, namespace_id)
        DEFERRABLE INITIALLY DEFERRED,
    ADD FOREIGN KEY (maintenance_generation_id, id) REFERENCES generations (id, namespace_id)
        DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE experiences (
    id              uuid PRIMARY KEY,
    namespace_id    uuid NOT NULL REFERENCES namespaces (id),
    source_version  integer NOT NULL DEFAULT 1 CHECK (source_version = 1),
    raw_text        text,
    source_metadata jsonb,
    content_digest  text,
    state           text NOT NULL CHECK (state IN ('retained', 'deleted')),
    created_by      uuid NOT NULL REFERENCES principals (id),
    received_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    UNIQUE (id, namespace_id),
    UNIQUE (id, namespace_id, source_version),
    CHECK (
        (state = 'retained' AND raw_text IS NOT NULL AND content_digest IS NOT NULL
            AND source_metadata IS NOT NULL AND deleted_at IS NULL
            AND octet_length(raw_text) BETWEEN 1 AND 65536)
        OR
        (state = 'deleted' AND raw_text IS NULL AND content_digest IS NULL
            AND source_metadata IS NULL AND deleted_at IS NOT NULL)
    )
);
CREATE INDEX experiences_ns_state_idx ON experiences (namespace_id, state, id);

CREATE TABLE jobs (
    id              uuid PRIMARY KEY,
    namespace_id    uuid NOT NULL,
    generation_id   uuid NOT NULL,
    experience_id   uuid NOT NULL,
    source_version  integer NOT NULL,
    input_digest    text,
    requested_by    uuid NOT NULL REFERENCES principals (id),
    authority_epoch bigint NOT NULL,
    state           text NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt         integer NOT NULL DEFAULT 0,
    retry_count     integer NOT NULL DEFAULT 0,
    lease_owner     text,
    lease_until     timestamptz,
    not_before      timestamptz NOT NULL DEFAULT now(),
    error_code      text,
    output_count    integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    UNIQUE (generation_id, experience_id, source_version),
    UNIQUE (id, namespace_id),
    UNIQUE (id, namespace_id, generation_id),
    FOREIGN KEY (generation_id, namespace_id) REFERENCES generations (id, namespace_id),
    FOREIGN KEY (experience_id, namespace_id, source_version)
        REFERENCES experiences (id, namespace_id, source_version),
    CHECK (state <> 'running' OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX jobs_state_not_before_idx ON jobs (state, not_before);
CREATE INDEX jobs_running_lease_idx ON jobs (lease_until) WHERE state = 'running';
CREATE INDEX jobs_generation_state_idx ON jobs (generation_id, state);

CREATE TABLE ingest_receipts (
    principal_id   uuid NOT NULL REFERENCES principals (id),
    namespace_id   uuid NOT NULL REFERENCES namespaces (id),
    key_digest     text NOT NULL,
    request_digest text NOT NULL,
    experience_id  uuid NOT NULL,
    initial_job_id uuid NOT NULL,
    accepted_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, namespace_id, key_digest),
    FOREIGN KEY (experience_id, namespace_id) REFERENCES experiences (id, namespace_id),
    FOREIGN KEY (initial_job_id, namespace_id) REFERENCES jobs (id, namespace_id)
);

CREATE TABLE artifacts (
    id             uuid PRIMARY KEY,
    namespace_id   uuid NOT NULL,
    generation_id  uuid NOT NULL,
    job_id         uuid NOT NULL,
    local_key      text NOT NULL,
    payload_schema text NOT NULL,
    payload        jsonb,
    display_text   text,
    search_text    text,
    state          text NOT NULL CHECK (state IN ('valid', 'invalidated')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, local_key),
    UNIQUE (id, namespace_id),
    FOREIGN KEY (job_id, namespace_id, generation_id) REFERENCES jobs (id, namespace_id, generation_id),
    CHECK (
        (state = 'valid' AND payload IS NOT NULL AND display_text IS NOT NULL AND search_text IS NOT NULL)
        OR
        (state = 'invalidated' AND payload IS NULL AND display_text IS NULL AND search_text IS NULL)
    )
);
CREATE INDEX artifacts_ns_gen_state_idx ON artifacts (namespace_id, generation_id, state);

CREATE TABLE artifact_sources (
    artifact_id    uuid NOT NULL,
    experience_id  uuid NOT NULL,
    source_version integer NOT NULL,
    namespace_id   uuid NOT NULL,
    spans          jsonb,
    PRIMARY KEY (artifact_id, experience_id, source_version),
    FOREIGN KEY (artifact_id, namespace_id) REFERENCES artifacts (id, namespace_id),
    FOREIGN KEY (experience_id, namespace_id, source_version)
        REFERENCES experiences (id, namespace_id, source_version)
);
CREATE INDEX artifact_sources_exp_idx ON artifact_sources (experience_id, source_version);

CREATE TABLE audit_events (
    id           bigserial PRIMARY KEY,
    event_type   text NOT NULL,
    actor_id     uuid,
    object_ids   jsonb NOT NULL DEFAULT '{}'::jsonb,
    safe_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    at           timestamptz NOT NULL DEFAULT now()
);
