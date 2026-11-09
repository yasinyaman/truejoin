# Benchmark: banka-heldout (single-table only) on duckdb

12 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 58.3% [32.0%, 80.7%] | 8.3% | 16.7% | 725 ms |
| catalog | 75.0% [46.8%, 91.1%] | 8.3% | 16.7% | 714 ms |
| semantic | 25.0% [8.9%, 53.2%] | 8.3% | 0.0% | 1110 ms |
| lexicon | 75.0% [46.8%, 91.1%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 25.0% [8.9%, 53.2%] | 0.0% | 0.0% | 1599 ms |
| lexicon+repair | 58.3% [32.0%, 80.7%] | 0.0% | 0.0% | 1294 ms |
| terms | 75.0% [46.8%, 91.1%] | 0.0% | 0.0% | 8 ms |
| terms+gaps | 66.7% [39.1%, 86.2%] | 0.0% | 0.0% | 12 ms |
| catalog+join-path | 58.3% [32.0%, 80.7%] | 0.0% | 0.0% | 1251 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| catalog+join-path vs raw | +0.0 pts | 2 | 2 | 1 |
| catalog+join-path vs catalog | -16.7 pts | 0 | 2 | 0.5 |
| catalog+join-path vs semantic | +33.3 pts | 4 | 0 | 0.125 |
| catalog+join-path vs lexicon | -16.7 pts | 1 | 3 | 0.625 |
| catalog+join-path vs semantic-schema | +33.3 pts | 4 | 0 | 0.125 |
| catalog+join-path vs lexicon+repair | +0.0 pts | 0 | 0 | 1 |
| catalog+join-path vs terms | -16.7 pts | 1 | 3 | 0.625 |
| catalog+join-path vs terms+gaps | -8.3 pts | 0 | 1 | 1 |

## Term layer gate

FAIL: catalog+join-path: it does not beat catalog (-16.7 points, needs more than +0.0). olap.terms must be off by default.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.