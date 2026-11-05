# 13. Nothing a model wrote is read until a reviewer certifies it

- Status: Accepted
- Date: 2026-11-05

## Context

A model is good at saying what things are called and bad at being asked on
every question: slow, different each time, and able to name what does not
exist. NVIDIA's Auto Ontology showed the useful half of this (join-path
checks, terms, expressions mined from query history) and left out the other:
its certified flag exists in the schema and nothing ever checks it.

Here the stakes are concrete. A relationship decides which joins are
accepted. A term decides what a question means. An expression decides what a
number is. A semantic type decides which column is masked.

## Decision

The model works at analysis time, on the schema, and its output is a draft.

| What the model writes | What makes it count | Who reads it then |
|---|---|---|
| Descriptions, semantic types | The table is approved | Masking labels, the published context |
| Relationships | Approved, and the data bears it out (`verified`) | The join graph |
| Terms: words for tables, columns, values | The term is certified | Semantic models, `answer_question`, `search_terms` |
| Expressions, written or mined | The expression is certified, apart from its term | Measures |

`DatabaseCatalog.certified_terms` is the one door: an approved catalog and a
certified term. Approving a table certifies its term, because the reviewer
was just shown it. Approving a whole catalog certifies none; doing that takes
`--certify-terms`, in so many words. What was certified survives re-analysis.

The model can only attach words to names that exist, and can name a mined
expression but never write or change one. A certified expression is still
held to a shape when it is used: a scalar over its own table's columns.

At query time no model is involved. `answer_question` matches words against
the certified vocabulary, so the same question gets the same answer, and
returns the request it understood and the SQL that ran.

The benchmark gate for the term layer was written before its arm existed
(`bench/README.md`). It passed: 37 of 48 against the catalog arm's 34, and
nothing invented. The accuracy lead is within noise; what separates the two
is that nothing is invented and that joins needing a table in between are
right.

## Consequences

- A fresh deployment answers questions in words only after a review. Until
  then `answer_question` says how to get there, and everything else works.
- Catalog analysis costs one more model call per table while `olap.terms` is
  on.
- A catalog certified while the server runs is read at the next start.
- The vocabulary is as good as its coverage: a word nobody certified matches
  nothing. In the benchmark the deterministic tool alone answers 27 of 48.
