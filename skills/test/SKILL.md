---
name: test
description: Write or run tests - find the project's test setup, add focused tests for new or changed behaviour, make them pass
---
# Tests

1. Find the setup: existing tests folder, pytest/unittest, jest/vitest, go test, cargo test, and how
   they're run (FORGE.md, package.json scripts, CI config). Match the style of the existing tests.
2. If there are no tests yet, pick the standard tool for the language and keep the setup minimal.
3. Write tests for behaviour, not internals:
   - the normal case, edge cases (empty, zero, very large, unicode), and the error cases
   - one thing per test, named after what it checks
   - no real network or paid APIs; use temporary folders for files
4. Run them. When a test fails, decide whether the test or the code is wrong before changing either.
   Never weaken an assertion just to make it pass.
5. Report what is covered, the command to run the tests, and the result (passed/failed counts).
