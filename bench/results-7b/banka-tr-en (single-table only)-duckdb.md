# Benchmark: banka-tr-en (single-table only) on duckdb

34 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 44.1% [28.9%, 60.5%] | 20.6% | 11.8% | 800 ms |
| catalog | 64.7% [47.9%, 78.5%] | 17.6% | 11.8% | 827 ms |
| semantic | 38.2% [23.9%, 55.0%] | 2.9% | 0.0% | 1090 ms |
| lexicon | 61.8% [45.0%, 76.1%] | 0.0% | 0.0% | 3 ms |
| semantic-schema | 35.3% [21.5%, 52.1%] | 0.0% | 0.0% | 1591 ms |
| lexicon+repair | 70.6% [53.8%, 83.2%] | 0.0% | 0.0% | 1250 ms |
| terms | 55.9% [39.5%, 71.1%] | 0.0% | 0.0% | 4 ms |
| catalog+join-path | 76.5% [60.0%, 87.6%] | 0.0% | 0.0% | 1265 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| catalog+join-path vs raw | +32.4 pts | 13 | 2 | 0.007385 |
| catalog+join-path vs catalog | +11.8 pts | 6 | 2 | 0.2891 |
| catalog+join-path vs semantic | +38.2 pts | 13 | 0 | 0.0002441 |
| catalog+join-path vs lexicon | +14.7 pts | 5 | 0 | 0.0625 |
| catalog+join-path vs semantic-schema | +41.2 pts | 14 | 0 | 0.0001221 |
| catalog+join-path vs lexicon+repair | +5.9 pts | 2 | 0 | 0.5 |
| catalog+join-path vs terms | +20.6 pts | 7 | 0 | 0.01562 |

## Term layer gate

PASS: catalog+join-path beats catalog by +11.8 points and named nothing that does not exist.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.