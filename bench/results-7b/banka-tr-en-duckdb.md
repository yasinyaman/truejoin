# Benchmark: banka-tr-en on duckdb

48 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 52.1% [38.3%, 65.5%] | 29.2% | 8.3% | 1047 ms |
| catalog | 70.8% [56.8%, 81.8%] | 20.8% | 8.3% | 1115 ms |
| semantic | 27.1% [16.6%, 41.0%] | 2.1% | 0.0% | 967 ms |
| lexicon | 43.8% [30.7%, 57.7%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 25.0% [14.9%, 38.8%] | 0.0% | 0.0% | 1684 ms |
| lexicon+repair | 50.0% [36.4%, 63.6%] | 0.0% | 0.0% | 1286 ms |
| terms | 56.2% [42.3%, 69.3%] | 0.0% | 0.0% | 4 ms |
| catalog+join-path | 77.1% [63.5%, 86.7%] | 0.0% | 0.0% | 1224 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| catalog+join-path vs raw | +25.0 pts | 17 | 5 | 0.0169 |
| catalog+join-path vs catalog | +6.2 pts | 8 | 5 | 0.5811 |
| catalog+join-path vs semantic | +50.0 pts | 24 | 0 | 1.192e-07 |
| catalog+join-path vs lexicon | +33.3 pts | 16 | 0 | 3.052e-05 |
| catalog+join-path vs semantic-schema | +52.1 pts | 25 | 0 | 5.96e-08 |
| catalog+join-path vs lexicon+repair | +27.1 pts | 13 | 0 | 0.0002441 |
| catalog+join-path vs terms | +20.8 pts | 11 | 1 | 0.006348 |

## Verdict

Fund the DSL through to joins and multi-stage queries: it beats raw text-to-SQL by 25.0 points (> 15.0).

## Term layer gate

PASS: catalog+join-path beats catalog by +6.2 points and named nothing that does not exist.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema | lexicon+repair | terms | catalog+join-path |
|---|---|---|---|---|---|---|---|---|
| correct | 25 | 34 | 13 | 21 | 12 | 24 | 27 | 37 |
| named something that does not exist | 0 | 0 | 11 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 3 | 4 | 0 | 4 |
| ran, but answered the wrong question | 22 | 10 | 20 | 27 | 32 | 19 | 21 | 7 |
| the language was written wrongly | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 |
| the query would not run | 1 | 4 | 0 | 0 | 1 | 1 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
