-- Merge fix (A1, BUILD.md merge task): unify bank-id representation on TEXT codes ("bank-01") across
-- every table. og.reservation.bank_id and og.grant.bank_id were `uuid`, inconsistent with
-- og.bank.bank_id/og.hub.bank_id (TEXT, 02b S4.2) and with the fleet twin, ledger and allocator, which
-- have only ever produced/consumed text bank codes (opengrid.fleet, opengrid.ledger.ReservationRecord,
-- opengrid.ledger.pg_backend). See orchestrator/src/opengrid/api/README.md "Known gap, not api's to
-- fix" and qa/merge-notes.md for the follow-up still owed by the core/allocator owners.
--
-- Forward-only (02b S9.6): widens uuid -> text, which is a lossless, backward-readable cast (any
-- existing uuid value round-trips through its canonical text form), so a rollback to older code that
-- still expects `uuid` can still parse these columns' values even though the declared type is now text.
--
-- NOTE for core owner (recorded in qa/merge-notes.md): `opengrid.core.models.engine.Reservation` and
-- `.Grant` still declare `bank_id: UUID` -- Pydantic will reject a real text bank code (e.g.
-- "bank-01") until that annotation becomes `bank_id: str`. `opengrid.allocator.__init__._to_grant_row`
-- also still does `bank_id=uuid5(NAMESPACE_URL, grant.bank_id)`, manufacturing a synthetic UUID instead
-- of writing the real bank code -- that call must change to `bank_id=grant.bank_id` once the model
-- field is `str`. Both files are outside the merge role's owned paths (core/platform/guardian/
-- allocator), so they are not edited here.

SET search_path TO og;

ALTER TABLE og.reservation
    ALTER COLUMN bank_id TYPE text USING bank_id::text;

ALTER TABLE og.grant
    ALTER COLUMN bank_id TYPE text USING bank_id::text;

-- Bank codes are always populated; keep the same NOT NULL contract as before (uuid columns had none
-- explicitly, but every write path has always supplied one) -- add it explicitly now that the type is
-- the same TEXT domain as og.bank.bank_id, so a future accidental NULL fails fast instead of silently
-- breaking the ledger/grant timeline join.
ALTER TABLE og.reservation
    ALTER COLUMN bank_id SET NOT NULL;

ALTER TABLE og.grant
    ALTER COLUMN bank_id SET NOT NULL;

-- Optional referential integrity now that both sides are the same type: bank_id must be a known bank.
-- NOT VALID + a separate VALIDATE would be the usual zero-downtime pattern on a large existing table;
-- MVP-S's og.reservation/og.grant are small enough (fresh deploy) to validate inline.
ALTER TABLE og.reservation
    ADD CONSTRAINT fk_reservation_bank FOREIGN KEY (bank_id) REFERENCES og.bank(bank_id);

ALTER TABLE og.grant
    ADD CONSTRAINT fk_grant_bank FOREIGN KEY (bank_id) REFERENCES og.bank(bank_id);
