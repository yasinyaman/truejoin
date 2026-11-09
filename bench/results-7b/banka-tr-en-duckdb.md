# Benchmark: banka-tr-en on duckdb

48 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 52.1% [38.3%, 65.5%] | 29.2% | 8.3% | 1028 ms |
| catalog | 70.8% [56.8%, 81.8%] | 20.8% | 8.3% | 1063 ms |
| semantic | 27.1% [16.6%, 41.0%] | 2.1% | 0.0% | 902 ms |
| lexicon | 64.6% [50.4%, 76.6%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 25.0% [14.9%, 38.8%] | 0.0% | 0.0% | 1599 ms |
| lexicon+repair | 50.0% [36.4%, 63.6%] | 0.0% | 0.0% | 1270 ms |
| terms | 100.0% [92.6%, 100.0%] | 0.0% | 0.0% | 4 ms |
| terms+gaps | 100.0% [92.6%, 100.0%] | 0.0% | 0.0% | 4 ms |
| catalog+join-path | 89.6% [77.8%, 95.5%] | 0.0% | 0.0% | 1214 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| catalog+join-path vs raw | +37.5 pts | 21 | 3 | 0.0002772 |
| catalog+join-path vs catalog | +18.8 pts | 12 | 3 | 0.03516 |
| catalog+join-path vs semantic | +62.5 pts | 30 | 0 | 1.863e-09 |
| catalog+join-path vs lexicon | +25.0 pts | 15 | 3 | 0.007538 |
| catalog+join-path vs semantic-schema | +64.6 pts | 31 | 0 | 9.313e-10 |
| catalog+join-path vs lexicon+repair | +39.6 pts | 19 | 0 | 3.815e-06 |
| catalog+join-path vs terms | -10.4 pts | 0 | 5 | 0.0625 |
| catalog+join-path vs terms+gaps | -10.4 pts | 0 | 5 | 0.0625 |

## Verdict

Fund the DSL through to joins and multi-stage queries: it beats raw text-to-SQL by 37.5 points (> 15.0).

## Term layer gate

PASS: catalog+join-path beats catalog by +18.8 points and named nothing that does not exist.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema | lexicon+repair | terms | terms+gaps | catalog+join-path |
|---|---|---|---|---|---|---|---|---|---|
| correct | 25 | 34 | 13 | 31 | 12 | 24 | 48 | 48 | 43 |
| named something that does not exist | 0 | 0 | 11 | 0 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 3 | 3 | 0 | 0 | 1 |
| ran, but answered the wrong question | 22 | 10 | 20 | 17 | 32 | 20 | 0 | 0 | 4 |
| the language was written wrongly | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| the query would not run | 1 | 4 | 0 | 0 | 1 | 1 | 0 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
