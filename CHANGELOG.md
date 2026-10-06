# Changelog

## 0.3.11

- Execute parameter batches one statement at a time so repeated UPDATE and DELETE
  flushes preserve earlier changes in the same transaction, and INSERT flushes
  can reuse keys deleted earlier in that transaction.
- Preserve the total affected-row count and compiled parameter-schema reuse.
