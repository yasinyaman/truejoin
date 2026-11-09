# Benchmark: banka-heldout on duckdb

16 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 50.0% [28.0%, 72.0%] | 31.2% | 12.5% | 910 ms |
| catalog | 62.5% [38.6%, 81.5%] | 18.8% | 12.5% | 833 ms |
| semantic | 18.8% [6.6%, 43.0%] | 6.2% | 0.0% | 1042 ms |
| lexicon | 56.2% [33.2%, 76.9%] | 0.0% | 0.0% | 3 ms |
| semantic-schema | 18.8% [6.6%, 43.0%] | 0.0% | 0.0% | 1587 ms |
| lexicon+repair | 43.8% [23.1%, 66.8%] | 0.0% | 0.0% | 1313 ms |
| terms | 81.2% [57.0%, 93.4%] | 0.0% | 0.0% | 6 ms |
| terms+gaps | 75.0% [50.5%, 89.8%] | 0.0% | 0.0% | 8 ms |
| catalog+join-path | 68.8% [44.4%, 85.8%] | 0.0% | 0.0% | 1298 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| catalog+join-path vs raw | +18.8 pts | 5 | 2 | 0.4531 |
| catalog+join-path vs catalog | +6.2 pts | 3 | 2 | 1 |
| catalog+join-path vs semantic | +50.0 pts | 8 | 0 | 0.007812 |
| catalog+join-path vs lexicon | +12.5 pts | 5 | 3 | 0.7266 |
| catalog+join-path vs semantic-schema | +50.0 pts | 8 | 0 | 0.007812 |
| catalog+join-path vs lexicon+repair | +25.0 pts | 4 | 0 | 0.125 |
| catalog+join-path vs terms | -12.5 pts | 1 | 3 | 0.625 |
| catalog+join-path vs terms+gaps | -6.2 pts | 0 | 1 | 1 |

## Verdict

Fund the DSL through to joins and multi-stage queries: it beats raw text-to-SQL by 18.8 points (> 15.0).

## Term layer gate

PASS: catalog+join-path beats catalog by +6.2 points and named nothing that does not exist.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema | lexicon+repair | terms | terms+gaps | catalog+join-path |
|---|---|---|---|---|---|---|---|---|---|
| correct | 8 | 10 | 3 | 9 | 3 | 7 | 13 | 12 | 11 |
| named something that does not exist | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 2 | 3 | 0 | 1 | 2 |
| ran, but answered the wrong question | 8 | 5 | 6 | 7 | 11 | 6 | 3 | 3 | 3 |
| the language was written wrongly | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| the query would not run | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
