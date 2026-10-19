# docs

The project blueprint is the reference for every decision. Key points for quick reading:

- Six business objectives: BO1, BO2, BO3, BO6 main; BO4, BO5 secondary.
- Six DSOs, one agent each for DSO1–DSO5 and a risk gate for DSO6.
- Every agent action passes the risk gate; tiers are fixed in `agents/flowguard/catalog.py`.
- Both modes (`agents`, `baseline`) run the same scenarios and write the same audit format.

## Decisions taken during the build

| Date | Step | Decision | Reason |
| --- | --- | --- | --- |
| 2026-10-19 | 0 | Workflows in `.github/workflows/`, scripts in `ci/` | GitHub only runs workflows from that folder |
| 2026-10-19 | 0 | Environments limited to `staging` and `prod` in the contracts | Matches the two namespaces |
| 2026-10-19 | 0 | Draft gate, audit, executor and agent stubs held back until step 5 | Not part of step 0 |
