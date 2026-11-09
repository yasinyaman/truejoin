# Benchmark: banka-tr-en (single-table only) on duckdb

34 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 44.1% [28.9%, 60.5%] | 20.6% | 11.8% | 804 ms |
| catalog | 64.7% [47.9%, 78.5%] | 17.6% | 11.8% | 806 ms |
| semantic | 38.2% [23.9%, 55.0%] | 2.9% | 0.0% | 1064 ms |
| lexicon | 91.2% [77.0%, 97.0%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 35.3% [21.5%, 52.1%] | 0.0% | 0.0% | 1533 ms |
| lexicon+repair | 70.6% [53.8%, 83.2%] | 0.0% | 0.0% | 1251 ms |
| terms | 100.0% [89.8%, 100.0%] | 0.0% | 0.0% | 4 ms |
| terms+gaps | 100.0% [89.8%, 100.0%] | 0.0% | 0.0% | 4 ms |
| catalog+join-path | 88.2% [73.4%, 95.3%] | 0.0% | 0.0% | 1274 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| catalog+join-path vs raw | +44.1 pts | 17 | 2 | 0.0007286 |
| catalog+join-path vs catalog | +23.5 pts | 10 | 2 | 0.03857 |
| catalog+join-path vs semantic | +50.0 pts | 17 | 0 | 1.526e-05 |
| catalog+join-path vs lexicon | -2.9 pts | 2 | 3 | 1 |
| catalog+join-path vs semantic-schema | +52.9 pts | 18 | 0 | 7.629e-06 |
| catalog+join-path vs lexicon+repair | +17.6 pts | 6 | 0 | 0.03125 |
| catalog+join-path vs terms | -11.8 pts | 0 | 4 | 0.125 |
| catalog+join-path vs terms+gaps | -11.8 pts | 0 | 4 | 0.125 |

## Term layer gate

PASS: catalog+join-path beats catalog by +23.5 points and named nothing that does not exist.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.