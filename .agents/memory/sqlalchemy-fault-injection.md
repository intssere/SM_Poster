---
name: SQLAlchemy fault injection
description: How connection wrappers can prevent transaction-failure tests from reaching their intended boundary.
---

Fault-injection tests must prove that they reached the intended transaction
boundary, not merely assert the expected public exception.

**Why:** SQLAlchemy catalog inspection recognizes registered connection types.
A transparent-looking connection proxy can fail inspection before any insert or
commit. That failure can be mistaken for simulated commit-acknowledgement loss.

**How to apply:** Preserve a real SQLAlchemy connection for catalog verification,
inject the fault at the transaction boundary, and use an independent connection
to assert durable consumption. Also assert that the uncertain caller did not
launch work and a later caller cannot replay it.