---
name: Credentialed test isolation
description: Why offline intent and synthetic fixture data do not establish isolation from attached media storage.
---

Treat tests in a credentialed workspace as capable of reaching attached services until isolation is enforced before collection. Synthetic creative IDs and local source bytes do not establish that storage is fake.

**Why:** An ostensibly offline media test selected the exposed runtime's real storage path and returned a verified object receipt. The recorded output could not distinguish a new object write from an existing-object read; zero-write safety could not be certified afterward.

**How to apply:** Use an explicitly isolated test environment, block real storage-client construction before collection, inject in-memory storage, and deny non-local network access. Keep loopback/Unix-socket access available for disposable PostgreSQL. Never infer that no remote mutation occurred merely because the input was test data; do not perform cleanup without separate authorization.