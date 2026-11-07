# Does the semantic layer earn its place?

A paired benchmark over one banking-shaped schema, 48 questions, half Turkish
and half English. Every arm answers the same question and returns the same
shape, so the runner cannot tell which is which. Grading is by **result set**
against a gold query, never by SQL text, because two correct queries rarely
look alike.

```bash
python -m bench.terms qwen2.5-coder:7b          # once: the catalog's terms, into bench/catalogs/
python -m bench.run_duckdb qwen2.5-coder:7b     # every arm on DuckDB, into bench/results/
pytest bench/tests                              # the harness itself
```

`OLLAMA_HOST` points both at a model on another machine, as it does for
ollama's own tools.

The two runs below are checked in under `bench/results-1.5b/` and
`bench/results-7b/`, named for the model that produced them; a fresh run
writes to `bench/results/`, which is ignored.

The thresholds that decide the outcome were **written down before the first
run** and are applied mechanically by `runner.verdict`, so the result is not
re-litigated after the fact.

## The arms

| Arm | How the request is chosen |
|---|---|
| `raw` | The model writes SQL from the DDL alone. |
| `catalog` | The model writes SQL from Warp's enriched `x-llm-context`. |
| `semantic` | The model writes a DSL request as free JSON. |
| `semantic-schema` | The same, with decoding constrained to a schema built from the semantic model. |
| `lexicon` | No model at all: words are matched against the semantic model. |
| `lexicon+repair` | The lexicon proposes; the model corrects it under the same constraint. |
| `terms` | No model: the product's `answer_question` tool over the catalog's certified terms. |
| `catalog+join-path` | Those terms propose, the declared keys supply the joins, the model corrects under the constraint. |

The last two exist only once `bench/catalogs/banka.json` has been generated.

## Results

### qwen2.5-coder:7b on the 48 questions

Execution accuracy on all 48, on the 34 that need no join and on the 14 that
do.

| Arm | 48q | single-table | join | Hallucinated | Median latency |
|---|---|---|---|---|---|
| raw | 52.1% | 44.1% | 10/14 | 29.2% | 1047 ms |
| catalog | 70.8% | 64.7% | 12/14 | 20.8% | 1115 ms |
| semantic | 27.1% | 38.2% | 0/14 | 2.1% | 967 ms |
| lexicon | 43.8% | 61.8% | 0/14 | **0%** | **2 ms** |
| semantic-schema | 25.0% | 35.3% | 0/14 | **0%** | 1684 ms |
| lexicon+repair | 50.0% | 70.6% | 0/14 | **0%** | 1286 ms |
| terms | 56.2% | 55.9% | 8/14 | **0%** | 4 ms |
| **catalog+join-path** | **77.1%** | **76.5%** | 11/14 | **0%** | 1224 ms |

> **Provenance.** Produced at commit `3d43434` with `qwen2.5-coder:7b`
> (ollama digest `dae161e27b0e`) at temperature 0, on an NVIDIA GB10 reached
> through `OLLAMA_HOST`. Two consecutive runs gave the first six arms the same
> answer to every question. The reports are in `bench/results-7b/`. The
> latencies are that machine's and do not compare with the tables below.

> **Since then (2026-11-07).** A term synonym of several words used to count
> when only its first word was in the question, so "customer transactions"
> claimed every question that said "customer". Fixed in the lexicon's scoring;
> re-run with no model, the `terms` arm answers **33 of 48 (68.8%)** where the
> table says 27 (six English questions fixed, none broken) and `lexicon` is
> unchanged. The arms that call the model, `catalog+join-path` among them,
> have not been re-run since.

What this run says, and what it does not, is [further down](#the-seventh-arm-what-the-run-said).

### Earlier runs: 40 questions, two tables

> **These two runs predate the seventh arm.** They were made on the first 40
> questions and two tables, before `q21` to `q24` and `subeler` were added,
> and have not been repeated on the 48.

Execution accuracy, 40 questions / the 34 that need no join. Six of the
questions require a join, which the DSL refuses by design in v1, so both
numbers are reported rather than averaged.

> **Provenance.** Every number below was produced at commit `ddc1d3a`. The
> commit after it, `28f2574`, changed `repair.py`, `arms.py`, `lexicon.py`,
> `runner.py` and `scoring.py` in response to a review, and the run was **not**
> repeated — so the `lexicon+repair` row in particular has not been re-measured
> against the code now in the tree. Reproduce with
> `python -m bench.run_duckdb qwen2.5-coder:7b bench/results-7b` (~30 min,
> needs a local ollama) before quoting these anywhere that matters.

**qwen2.5-coder:1.5b**

| Arm | 40q | single-table | Hallucinated | Median latency |
|---|---|---|---|---|
| raw | 22.5% | 26.5% | 27.5% | 482 ms |
| catalog | 47.5% | 50.0% | 15.0% | 474 ms |
| semantic | 15.0% | 17.6% | 15.0% | 1400 ms |
| **lexicon** | **52.5%** | **61.8%** | **0%** | **2 ms** |
| semantic-schema | 32.5% | 38.2% | **0%** | 1748 ms |
| lexicon+repair | 27.5% | 32.4% | **0%** | 2039 ms |

**qwen2.5-coder:7b**

| Arm | 40q | single-table | Hallucinated | Median latency |
|---|---|---|---|---|
| raw | 45.0% | 41.2% | 32.5% | 2864 ms |
| catalog | 75.0% | 70.6% | 27.5% | 3726 ms |
| semantic | 37.5% | 44.1% | 2.5% | 4733 ms |
| lexicon | 52.5% | 61.8% | **0%** | **3 ms** |
| semantic-schema | 40.0% | 47.1% | **0%** | 8365 ms |
| **lexicon+repair** | **70.0%** | **82.4%** | **0%** | 7909 ms |

## What the runs established

**Model size is the dominant variable, and it argues against the DSL on its
own.** From 1.5b to 7b the catalog arm gained 27.5 points and the constrained
DSL arm 7.5. The SQL arms convert model capacity into accuracy; the DSL arm
barely does. The pre-registered verdict fires on the full question set —
`lexicon+repair` trails `catalog` by 5.0 points there, because six of the forty
need a join the DSL refuses outright. On the 34 it can answer, that reverses to
+11.8, which is why both numbers are reported.

**Constraining decoding removes form errors completely, and only matters while
they exist.** The unconstrained semantic arm failed 17 of 40 on form alone:
7 metrics written `sum(tutar)` instead of `tutar:sum`, 6 names that did not
exist, 4 `order_by` values handed over as lists. A JSON schema derived from the
semantic model — one `oneOf` branch per table, one per measure, values
enumerated for categorical dimensions — made every one of those
unrepresentable, worth +17.5 points at 1.5b. At 7b it is worth +2.5, because a
7b model does not make those mistakes. It still takes hallucination to zero,
and it still costs roughly double the latency.

**Instructions hurt a small model; constraints help it.** An earlier version of
the constrained prompt spelled out the pragmatics a schema cannot carry — use a
dimension for every "per X", a filter for every condition, `cumsum` for a
running total. It made the arm *worse*: 13/40 correct with two sentences
against 10/40 with the list, and one runtime failure against seven. Constrain
the output; do not lecture the model.

**A lexicon is a serious baseline, not a straw man.** `lexicon.py` matches
folded words against the semantic model — Turkish `başarılı` against the stored
`basarili` — and beats every 1.5b arm outright. It answers in 2 ms, can never
name something that does not exist, and returns the same number on every run:
52.5% in both tables above, which is what "deterministic" looks like in a
results table. What it cannot do is generalise — its vocabulary is per schema
and per language.

**A model can write that vocabulary, which is where the model belongs.**
`lexicon_gen.py` spends one completion on the *schema* rather than one per
question, and the result is a JSON file: reviewable before use, diffable in
version control, correctable by whoever owns the data, and absent at query
time. Generation is itself constrained to the names that exist, so it can say
what a column is called but never invent one. The generated lexicon scores
21/40 against the hand-written 20/40, from a single 37-second call.

Getting that schema right took two tries, in opposite directions. Requiring a
word for every name produced `tutar: ["tutar", " tutar"]` — with no way to say
"nothing to add", the model filled the array with inflections of the name.
Dropping the requirement made it skip `segment` and `sube_kodu` entirely rather
than consider them. Every name is now required, and allowed to be empty.

**Deterministic failures are near-misses; model failures are not.** Of the
lexicon's wrong answers, roughly half are a single field from right — it drops
one condition because it had no word for it. Of `semantic-schema`'s, almost
none are. A copilot lives on that difference: correct one field, or start
again. `repair.py` measures it, and the same neighbourhood is a ready-made "did
you mean" menu — computable for a structured request, not for a SELECT.

**Seeding the model with the deterministic answer beat everything.**
`lexicon+repair` reached 82.4% on single-table questions with zero
hallucination, against the catalog arm's 70.6% with 20.6%. Against the lexicon
alone it is +20.6 points with **7 questions fixed and none broken**
(p = 0.016); against the unconstrained DSL arm +38.2 (p = 0.0002). Each part
does what it is good at: word matching finds the columns and values, the model
judges whether that is what was asked, the schema stops either from inventing
anything.

**And it is sharply model-size dependent — measured, not assumed.** The same
arm on 1.5b scores 27.5% where the lexicon alone scores 52.5%: it fixed 1
question and **broke 11**, p = 0.006. A small model does not correct a good
deterministic answer, it vandalises it. `RepairArm(fall_back=True)` keeps the
proposal when a correction will not run, and `Answer.fell_back` records that it
did — but nothing stops a model from confidently replacing a right answer with
a wrong one, so below some capacity this design is worse than no model at all.

## The seventh arm: what was fixed before it ran

The term layer ships behind a gate, and the gate was written before the arm.
Everything in this section was committed first; the numbers were added after.

**The arm.** `catalog+join-path` answers from the catalog's certified terms:
the vocabulary a model wrote once per table and a reviewer certified. Word
matching against that vocabulary proposes a request. A dimension or a filter
that lives in another table is reached along the foreign keys the database
declares, never along a join the request wrote. The model then corrects the
proposal under a decoding constraint built from the same catalog, and the
request runs through `query_metrics`. A second row, `terms`, is the same
without the model, which is what the `answer_question` tool does; it is
reported, not gated.

**The questions.** Eight were added, all needing a join, for 48 in total and
14 with a join: `q21` to `q24`, in Turkish and English. `q21` and `q23` need
`subeler`, a table `islemler` reaches only through `musteriler`. The fixture
gained that table for them; no existing gold answer changed.

**The thresholds**, applied by `runner.term_gate` on `qwen2.5-coder:7b`:

- the arm beats `catalog` on all 48 questions (a gap above 0 points), and
- it names nothing that does not exist on any of them (0% hallucinated).

Failing either leaves `olap.terms` off by default.

**What was built after that.** The planner moved into the product
(`trio.application.olap.lexicon`) and learned three things, each a general
rule rather than a case: a breakdown or a condition may come from a table the
measured one references; a sum is of the table that holds the number; a count
is of the noun it is attached to ("işlem sayısı"). A column-name part that
only repeats its table's name stopped counting as a mention. These were
written with the eight new questions in view, which is the caveat the
hand-written lexicon already carries. The vocabulary is not hand-written:
`python -m bench.terms` runs the product's own term extraction over the
fixture's catalog and the output is checked in unedited.

## The seventh arm: what the run said

**The gate passed, and the accuracy half of it is a tie.** `catalog+join-path`
answered 37 of 48 against the catalog arm's 34: +6.2 points, with eight
questions only it got right and five only the catalog arm did. McNemar's exact
p is 0.58. By the rule written beforehand that is a pass; as evidence that it
is the more accurate arm it is not one. What does separate the two:

- **Nothing invented.** No answer of the term arm names a table or column
  that does not exist, on any question. Four of the catalog arm's queries did
  not run at all.
- **A table in between.** On the four questions that need `musteriler` to get
  from `islemler` to `subeler` it is 4 of 4 against 2 of 4. The path is read
  from the keys, so there is no join to get wrong.
- **Not joins in general.** On all 14 join questions it is one behind, 11
  against 12. It cannot yet give two breakdowns (`q20`), and the model broke
  one correct proposal (`q22` in English).

**Two of the three questions it leads by come from the table this phase
added.** `q08`, customers per branch, needs no join. With `subeler` in the
schema the catalog arm joins it anyway and writes an unqualified `sube_kodu`,
which does not run; on the two-table fixture it answered both languages
correctly. That is a real way text-to-SQL degrades as a schema grows, and it
is also a change made here, for this arm's questions. Without `q08` the lead
is one question.

**The model is what makes the arm.** `terms`, the same vocabulary with no
model, is the `answer_question` tool as it ships: 27 of 48, and 17 of 24 in
Turkish against 10 of 24 in English, which is the vocabulary's own coverage.
Given that proposal and the constraint, the model fixed eleven answers and
broke one. That is the copilot shape the earlier runs argued for, now with
joins: the tool answers and shows its reading, and a model corrects the
reading through `query_metrics`.

**What it still gets wrong.** Eleven questions: the top-N and ranking ones
(`q07`, `q15`, `q16`), half of them by ordering on a column the request did
not select; a condition on a missing value, for which there is no word
(`q17`); two breakdowns (`q20`); and `q22` above.

**What it decides.** The rule is met, so `olap.terms` is on by default. That
is the rule applied as written, with the reading above known when it was
applied. What on costs is one more model call per table at catalog analysis;
what it changes at query time is nothing, until a reviewer has certified a
term.

## Caveats

- **The 48-question run and the two earlier ones are not one experiment.** A
  table, eight questions, the planner and the machine all changed between
  them. `lexicon+repair` fell from 28 to 24 of the 34 single-table questions
  across that change and the cause was not isolated.

- **N = 40**, over a 10-row fixture. Confidence intervals are wide and mostly
  overlapping. The comparisons that separate cleanly are `lexicon+repair`
  against `lexicon` (in both directions, one run each), against `semantic` and
  against `semantic-schema`. `lexicon+repair` against `catalog` does **not**
  separate (p = 0.29 on the subset): read the accuracy gap as a tie and the
  hallucination gap as the real difference.
- **The lexicon was corrected against this question set; the LLM arms were
  not.** Its first untuned score was 15/40. Read its number as optimistic in a
  way the others' are not.
- **One-edit repair is judged on gold rows**, so on a small fixture a
  coincidentally matching neighbour counts as a repair.
- **Hallucination rises with model size on the SQL arms** (catalog 15% → 27.5%)
  while only one query failed to run at 7b — so the flag there is mostly
  aliasing, not broken SQL, and should not be read as it is at 1.5b.
- **Oracle and SQL Server arms are wired and unrun.** `seed_oracle.sql` and
  `models.oracle_executor` are in place; the run needs a container.

## Layout

| File | |
|---|---|
| `arms.py` | The arms, and the one execution path they share. |
| `constrain.py` | The semantic model as a decoding constraint. |
| `lexicon.py` | The hand-written vocabulary, over the product's word-matching planner. |
| `lexicon_gen.py` | A model writing the lexicon, once per schema. |
| `repair.py` | One-edit repair: how much work a wrong answer leaves. |
| `terms.py` | The fixture's catalog, and the terms a model writes for it. |
| `catalogs/` | That catalog with its terms, checked in for review. |
| `scoring.py`, `stats.py` | Grading, Wilson intervals, McNemar exact. |
| `runner.py` | Running arms, pairing them, applying the thresholds. |
| `questions/` | The question set, as data. |
| `lexicons/` | Generated vocabularies, checked in for review. |
