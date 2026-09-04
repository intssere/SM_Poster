# Task #39 Operator Runbook

This runbook covers the disabled-live-publishing manual dispatch foundation. `PUBLISH_UNKNOWN` MUST NOT be retried.

| Condition | Is another POST safe? | Operator action | Reconciliation/provider verification | Audit/escalation |
|---|---|---|---|---|
| `PUBLISH_FAILED` | Only explicit reschedule after review | Inspect safe error and readiness, then reschedule or cancel | No provider reconciliation unless outcome is ambiguous | Record operator decision; escalate repeated failures |
| `PUBLISH_UNKNOWN` | **Prohibited** | Verify Pinterest directly, then confirm Pin or cancel unknown | Explicit reconciliation required; provider verification required for Pin confirmation | Preserve attempt/reconciliation audit; escalate before any write |
| `PUBLISHED_STATE_PERSISTENCE_UNKNOWN` | Prohibited | Verify provider Pin and reconcile persistence | Provider verification required | Escalate to data-integrity owner |
| `PublicationReconciliationError` | Prohibited until resolved | Preserve transaction evidence and investigate | Reconciliation retry only after rollback/root-cause review | Escalate immediately |
| `TOKEN_DECRYPT_FAILED` | Prohibited | Repair server credential configuration; do not resubmit | No provider call is safe | Security escalation; credentials remain server-side |
| `MEDIA_NOT_PUBLISHABLE` | Not until corrected | Replace with approved public HTTPS media and regenerate snapshot | Re-run readiness | Audit provenance change |
| `PUBLISHING_SCOPE_REQUIRED` | Not until separately authorized | Obtain reviewed write-scope authorization; reconnect if needed | Verify exact account/board | Security and product approval required |
| `PUBLISHING_DISABLED` | Prohibited | Keep publishing disabled; no operator bypass | None | No live call; document decision |
| `DESTINATION_MISMATCH` | Prohibited | Recheck connection, board, and immutable snapshot | Verify exact destination | Escalate identity mismatch |
| `INVALID_APPROVAL` | Prohibited | Re-review and create a correctly bound approval | Verify revision/creative identity | Preserve immutable audit |
| `INVALID_CREATIVE` | Prohibited | Repair provenance or create a new approved snapshot | Verify authentic source image | Escalate if identity cannot be proven |

All actions require authenticated human operators. Never expose tokens, Authorization headers, ciphertext, raw provider bodies, or fingerprints.
