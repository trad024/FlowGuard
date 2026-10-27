# LogChunks: evidence localization (D32)

There is no manual labelling. D32 (2026-10-27) replaced the single-labeller plan (D26a): the
labelling sheet, the re-label generator and the intra-rater kappa were dropped.

LogChunks is used for **evidence localization only**. Each of its logs comes with a
human-labelled chunk, the text that explains why the build failed. For each log in the seeded
sample (`../logchunks-sample-150.csv`), the lines Diagnose quotes as evidence are compared with
that chunk:

| Metric | Definition |
| --- | --- |
| Hit rate (H3 primary) | Share of logs where at least one quoted line is inside the chunk |
| Line precision | Quoted lines inside the chunk / quoted lines |
| Line recall | Chunk lines quoted / chunk lines |

Chunks are given as text, not line numbers, so each is located in its log by line matching
(whitespace collapsed, ANSI codes and carriage-return overwrites removed). On the sample, 96 of
150 chunks are located fully, 34 partly and 20 not at all; those 20 are excluded and reported.

The 5-class root-cause accuracy is measured only on the generated corpus. Limitation stated in
the report: it has no external validation on real logs.

The raw logs are not in git. Get them with `py evaluation\datasets.py download`; each sample
row's `log_path` is relative to `evaluation/datasets/raw/LogChunks/logs/`, and `log_sha256`
confirms you are reading the same file.
