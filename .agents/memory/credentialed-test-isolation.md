---
name: Credentialed test isolation
description: Why offline intent and synthetic fixture data do not establish isolation from attached media storage.
---

Treat tests in a credentialed workspace as capable of reaching attached services until isolation is enforced before collection. Synthetic creative IDs and local source bytes do not establish that storage is fake.

**Why:** An ostensibly offline media test selected the exposed runtime's real storage path and returned a verified object receipt. The recorded output could not distinguish a new object write from an existing-object read; zero-write safety could not be certified afterward.

**How to apply:** Use an explicitly isolated test environment, block real storage-client construction before collection, inject in-memory storage, and deny non-local network access. Keep loopback/Unix-socket access available for disposable PostgreSQL. Never infer that no remote mutation occurred merely because the input was test data; do not perform cleanup without separate authorization.

When adding tests that enforce their isolated runner, audit every broad test-suite consumer, including matrix-conditional jobs.

**Why:** A successful focused job or one database version can coexist with a failing full-suite job on another version; enforced-isolation fixtures correctly refuse an unwrapped legacy invocation.

**How to apply:** Run those cases through the isolated runner, or document their exclusion from the unwrapped command and execute every excluded case in a separate required no-skips job. Never bypass the isolation refusal to make legacy CI green.