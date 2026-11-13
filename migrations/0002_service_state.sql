-- P0-5: fail-closed recovery gate. A database restored from backup starts 'quarantined': every request path that
-- reads or writes memory (submit, search, reads, worker claim/finalize) is refused until an operator releases it
-- after the policy ledger has been re-applied and verified (docs/operations/backup-and-recovery.md).
ALTER TABLE system_control
    ADD COLUMN service_state text NOT NULL DEFAULT 'serving'
        CHECK (service_state IN ('serving', 'quarantined')),
    ADD COLUMN service_state_changed_at timestamptz NOT NULL DEFAULT now();
