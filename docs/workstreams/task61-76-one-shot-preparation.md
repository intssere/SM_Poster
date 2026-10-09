# Task 61.76 — production five-pin preparation, single-execution safety contract

Status: **engineering design / NO PRODUCTION EXECUTION**. Base: `bf333aedf656f27208a495969c3f92993a6d0338`. This document does not grant publishing admission.

## Verified baseline (9 October 2026)

Railway production deployment `648264a2-9404-47a9-a986-bd4f8b5c563a` reached SUCCESS on that commit. Its read-only `FIVE_PIN_BOUNDED_PREFLIGHT_V1` recorded PASS, exactly five selected eligible candidates, additional ready candidates, canonical schema 0034, routine PAUSED, zero PUBLISH_UNKNOWN and zero nonterminal batches. Independent READY-batch certification refused because no READY batch existed. All provider and database write counters in those startup certifications were zero.

## Existing production primitives

- `app.state_transfer.bounded_pilot_preflight.run()`: one read-only repeatable-read database transaction; returns private plan and candidate identity/fingerprint in memory; suppress those fields in production logs.
- `app.services.routine_bounded_preparation.validate_preflight_receipt(...)`: SELECT FOR UPDATE and recompute candidate identities against supplied preflight receipt.
- `app.services.routine_bounded_batch.create_batch(...)`: creates an OPEN batch and commits immediately.
- `app.services.routine_bounded_preparation.prepare_batch(...)`: freezes five entries and **commits intermediate writes**; creates autonomous creative/publication/permit records under process-local gated settings; seals READY or closes FAILED.
- `app.state_transfer.ready_bounded_batch_certification.run()`: independent read-only READY batch and five-entry manifest certification.

Do not treat a passing read-only preflight as proof that the mutating preparation will succeed. Production container was previously missing DejaVu fonts; PR #234 added the package and a build-time font check. The resulting preflight passed.

## Fail-closed one-shot acceptance criteria

1. Single invocation only: use an explicit durable unique operation key and atomically reserve it in PostgreSQL **before** the first OPEN batch creation. This key must be unique independently of selected candidate fingerprint and resilient to restarts, duplicate CLI launches, and crash recovery. Repeated invocations of the same authorized operation return its final sanitized outcome without creating another batch; **never** automatically retry a FAILED/partial attempt.
2. Before every write: require exact deployed SHA/release provenance and a fresh, uncached `FIVE_PIN_BOUNDED_PREFLIGHT_V1` PASS with schema 0034, PAUSED, no nonterminal batches, no PUBLISH_UNKNOWN, one ACTIVE October 2026 plan, exactly five candidates, and publishing_admission NOT_GRANTED. Disallow a PASS from previous process startup as an input substitute.
3. Perform gate validation before reservation. Gate configuration cannot be persisted as enabled beyond the controlled invocation. Preserve all unrelated scheduler, worker, Pinterest/Buffer and board-ensure gates as disabled.
4. Persist a **sanitized** audit record for the operation ID, expected preflight fingerprint, operation state, and eventual batch ID. Candidate/private product IDs remain internal only. Do not overwrite or alter historical FAILED batch `e2159a35-bf48-57a9-86a6-255497eb8d77` or any frozen entries.
5. Re-derive and lock the exact five candidate/board identities inside the mutating transaction via `validate_preflight_receipt`. Refuse on any mismatch; do not silently select replacements.
6. Admit exactly one new OPEN batch, then prepare exactly five entries. No publishing attempt, provider/Buffer/Pinterest calls, provider reads, OAuth calls, scheduler/worker activation or autonomous dispatch. Creating internal creative/publication/approval/permit records is the explicitly authorized scope, not provider execution.
7. On any partial failure, freeze the operation as failed. The batch should be FAILED when safely possible; never create a second batch to compensate. Record and report uncertainty rather than assume rollback: `prepare_batch` has intermediate commits.
8. After preparation, run the independent read-only READY certification; require PASS, exactly one READY nonterminal batch, exactly five entries, correct manifest/fingerprints, zero reserved provider attempts, five future schedules, and publishing_admission NOT_GRANTED.
9. Restore temporary deployment config to the exact baseline, verify the settled production source and 1/1 application/DB health, then obtain fresh read-only post-operation evidence. Never publish.
10. Emit a sanitized fixed-schema report with success, stage, static refusal code, operation ID/fingerprint, batch ID/manifest digest (only if admitted), entry count, preparation and certification result, provider call counts (zero), and final gate/provenance checks. No secrets, URLs, raw identities, SQL exceptions or stack traces.

## Implementation / test order

- Introduce a durable PostgreSQL operation reservation with unique key, enforced single-use even after a crash or FAILED preparation; migration and rollback/adoption must be tested on PostgreSQL 16, 17 and 18. Prefer an already existing durable one-shot ledger if one meets these guarantees; audit it rather than duplicate it.
- Add an explicit CLI runner guarded by an exact operation token; no HTTP route, cron, predeploy hook, ordinary application startup path or autonomous worker path may invoke it.
- Dependency-inject preflight, session, preparation, and READY certification for deterministic regression tests. Add tests for double invocation, parallel contention, fingerprint mismatch, stale plan/date, disabled gates, injected failure at each intermediate commit, partial READY certification, and total provider-call isolation.
- Prove no autonomous/provider dispatch is reachable during preparation by inspecting service call graph and mocked boundaries. Separate process-local preparation settings from live environment configuration. Do not infer provider-free behavior from the CLI name.
- Run all 14 GitHub workflows and verify exact head/base before presenting an exact-head merge authorization.
- Separately certify a **one-time Railway execution mechanism** with minimal, reversible changes and guaranteed removal, ideally a controlled job that cannot repeat on app restart or automatic deploy. Avoid using a normal `preDeployCommand` as the only one-shot guard.
- Only after implementation CI, merge authorization, deployed provenance validation and a **fresh production preflight** may the already-authorized bounded operation be considered. An additional approval is required for any changed scope or provider operation.

## Current prohibited operations

No merging without exact-head authorization; no production schema/config mutation without verified execution controls; no automatic re-preparation; no Pinterest/Buffer execution; no recurring dispatch; no publishing admission; no modification of historical FAILED evidence.

## Exit criteria

Engineering is complete only when tested durable one-shot semantics, verified provider-free preparation code paths, and a certified reversible production execution mechanism exist. Production Task 61.76 is complete only after exactly one preparation attempt yields an independently certified READY five-entry batch, with sanitized audit evidence and unchanged closed-state publishing gates.
