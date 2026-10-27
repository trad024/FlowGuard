# Labelling the LogChunks sample (D14)

One labeller labels all 150 logs (`../logchunks-sample-150.csv`) in a copy of
`logchunks-labels-blank.csv` (D26a). At least 7 days after finishing, `evaluation/relabel.py`
writes a blind sheet for a seeded random 50, in a shuffled order and without the first answers;
the labeller fills it without looking at the first pass. The intra-rater Cohen's kappa, and the
comparison of the evidence lines with LogChunks' own labelled chunks, are Step 4b. If a second
labeller becomes available, they label the same 50.

The raw logs are not in git. Get them with `py evaluation\datasets.py download`; each sample
row's `log_path` is relative to `evaluation/datasets/raw/LogChunks/logs/`, and `log_sha256`
confirms you are reading the same file.

## Columns

| Column | Fill in |
| --- | --- |
| `labeller` | Your GitHub user name |
| `root_cause` | One of the five classes below, or `unclear` |
| `confidence_1_to_3` | 1 guess, 2 fairly sure, 3 certain |
| `evidence_lines` | Line numbers in the log that justify the label, e.g. `812-818` |
| `notes` | Anything that made the case hard |

## Classes (same as `contracts.RootCause` and the CI failure generator)

| Class | The build failed because… | Typical evidence |
| --- | --- | --- |
| `code` | the project's own code or tests are wrong | compile error in project files, assertion failure, lint violation |
| `dependency` | a dependency cannot be resolved or is incompatible | package not found, version conflict, breaking upstream release |
| `environment` | the build environment does not match what the code needs | wrong language or tool version, missing system package, bad configuration |
| `flaky_test` | a test fails non-deterministically, unrelated to the change | timeout or race in a test, passes on rerun |
| `infrastructure` | the CI service or the network failed | runner died, registry or mirror unreachable, out of disk, job killed |

Label what caused the failure, not what the log line looks like: a timeout inside a unit test
is `flaky_test`, a timeout downloading packages is `infrastructure`. Use `unclear` rather than
guessing when the log does not show the cause.
