# Does the semantic layer earn its place?

A paired benchmark over one banking-shaped schema, 48 questions, half Turkish
and half English. Every arm answers the same question and returns the same
shape, so the runner cannot tell which is which. Grading is by **result set**
against a gold query, never by SQL text, because two correct queries rarely
look alike.

```bash
python -m bench.terms qwen2.5-coder:7b          # once: the catalog's terms, into bench/catalogs/
python -m bench.run_duckdb qwen2.5-coder:7b     # every arm on DuckDB, into bench/results/
python -m bench.heldout                         # the no-model arms, tuned set vs held-out, in seconds
python -m bench.followup                        # follow-up questions, no model, in seconds
python -m bench.unreadable qwen2.5-coder:7b     # questions the vocabulary reads none of, a minute
python -m bench.pilot                           # a pilot report on 20 untuned questions, in seconds
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
| raw | 52.1% | 44.1% | 10/14 | 29.2% | 1138 ms |
| catalog | 70.8% | 64.7% | 12/14 | 20.8% | 1216 ms |
| semantic | 27.1% | 38.2% | 0/14 | 2.1% | 1085 ms |
| lexicon | 64.6% | 91.2% | 0/14 | **0%** | **2 ms** |
| semantic-schema | 25.0% | 35.3% | 0/14 | **0%** | 1724 ms |
| lexicon+repair | 50.0% | 70.6% | 0/14 | **0%** | 1367 ms |
| **terms** | **100%** | **100%** | **14/14** | **0%** | 4 ms |
| terms+gaps | 100% | 100% | 14/14 | **0%** | 4 ms |
| catalog+join-path | 89.6% | 88.2% | 13/14 | **0%** | 1406 ms |

> **Provenance (2026-11-09).** Produced at commit `1d8d857` with
> `qwen2.5-coder:7b` (ollama digest `dae161e27b0e`) at temperature 0, on the
> same NVIDIA GB10 reached through `OLLAMA_HOST`; the reports in
> `bench/results-7b/` are this run's. Every arm that calls the model on its
> own (`raw`, `catalog`, `semantic`, `semantic-schema`, `lexicon+repair`)
> gave the same number as on 2026-11-08: what moved is the planner. `terms`
> is in-sample (its rules were written from these misses); the held-out
> numbers [below](#the-held-out-set) are the ones to quote. What the change
> in `catalog+join-path` means is [further down](#the-model-now-subtracts).
>
> **Re-run at `acfbb5a` (2026-11-09, evening).** After the refusal of
> unreadable questions and the fix to value words, all three sets were run
> again with the same model and machine. Every arm, on every question, gave
> the same verdict as at `1d8d857` (tuned and held-out) and `d74abf9`
> (unreadable, `terms` now 0 with the model run too); only latencies moved.
> The reports in `bench/results-7b/` are this run's.

The history of the earlier table, for the record:

> **First run.** Produced at commit `3d43434` with `qwen2.5-coder:7b`
> (ollama digest `dae161e27b0e`) at temperature 0, on an NVIDIA GB10 reached
> through `OLLAMA_HOST`. Two consecutive runs gave the first six arms the same
> answer to every question. The reports are in `bench/results-7b/`. The
> latencies are that machine's and do not compare with the tables below.

> **Since then (2026-11-07).** A term synonym of several words used to count
> when only its first word was in the question, so "customer transactions"
> claimed every question that said "customer". Fixed in the lexicon's scoring;
> re-run with no model, the `terms` arm answers **33 of 48 (68.8%)** where the
> first run said 27 (six English questions fixed, none broken) and `lexicon` is
> unchanged. Then a number named with no word for how ("monthly transaction
> amount", no "total") was answered as a count of rows broken down by it; it
> now takes the measure's own aggregation, and q12 is right in both languages:
> `lexicon` **23 of 48 (47.9%)**, `terms` **35 of 48 (72.9%)**, none lost.
>
> **Re-run with the model (2026-11-08, commit `11ec61e`).** The same model
> and digest on the same machine, against the same `bench/catalogs/banka.json`.
> Every arm that calls the model gave the same answer to every question as the
> first run: `raw` 25, `catalog` 34, `semantic` 13, `semantic-schema` 12,
> `lexicon+repair` 24 and `catalog+join-path` **37 of 48**. The fixes moved
> only the arms without a model (`lexicon` 21 → 23, `terms` 27 → 35); where
> `catalog+join-path` takes those proposals, the model was already correcting
> them. The term gate still passes (+6.2 over `catalog`, nothing invented)
> and the verdict is unchanged. `lexicon+repair` was measured in a run of its
> own, because the network to the model dropped while the full run reached it;
> in that run one question (`q07-en`, wrong before as well) still timed out.
> The reports from these runs were replaced by the 2026-11-09 run's.
>
> **The planner, taught from its misses (2026-11-08, after `53a109f`).**
> `check_question` named the words the vocabulary did not cover in the
> wrong answers; the planner now reads them (breakdown words, two
> breakdowns, rankings, absence, short codes in capitals, what "distinct"
> counts, "the N <things>"). With no model, `terms` answers **48 of 48** and
> `lexicon` 31, none lost. Those rules were written against these very
> questions, so 48 is an in-sample number: on the 16 held-out questions
> (below), `terms` went from 4 to 12 right and `lexicon` from 2 to 9. The
> arms that call the model were re-run on 2026-11-09: the table above.

### The held-out set

`questions/banka-heldout.json` holds 16 questions over the same schema,
written after the planner was tuned on the 48 and never used to tune it. The
48 measure how well the planner was fitted to them; these measure how well it
reads. `python -m bench.heldout` runs the two arms that need no model on both
sets and lists the held-out misses:

| | tuned set (48) | held-out (16) |
|---|---|---|
| `terms`, before (`53a109f`) | 35 | 4 |
| `terms`, after (`ff75ead`) | 48 | 12 |
| `lexicon`, before | 23 | 2 |
| `lexicon`, after | 31 | 9 |
| `terms`, transforms over joins | 48 | 13 |

Every arm, model included (2026-11-09, `1d8d857`, the same model and machine;
`bench/results-7b/banka-heldout-duckdb.md`):

| Arm | 16q | single-table | join | Hallucinated |
|---|---|---|---|---|
| raw | 8 | 7/12 | 1/4 | 31.2% |
| catalog | 10 | 9/12 | 1/4 | 18.8% |
| semantic | 3 | 3/12 | 0/4 | 6.2% |
| lexicon | 9 | 9/12 | 0/4 | **0%** |
| semantic-schema | 3 | 3/12 | 0/4 | **0%** |
| lexicon+repair | 7 | 7/12 | 0/4 | **0%** |
| **terms** | **13** | 9/12 | **4/4** | **0%** |
| terms+gaps | 12 | 8/12 | **4/4** | **0%** |
| catalog+join-path | 11 | 7/12 | **4/4** | **0%** |

Run it with `python -m bench.run_duckdb qwen2.5-coder:7b bench/results-7b
bench/questions/banka-heldout.json`. Sixteen questions are few: every
interval is wide (`terms` 57-93%), and of the report's paired comparisons
(`catalog+join-path` against each other arm) only those with the two
semantic arms fall below p = 0.05.

The rule that keeps the set worth having: **do not change the planner to pass
one of these questions.** A miss here is a sample of what the planner does not
read; teach it from new misses on the tuned set or from real usage, then look
here. A held-out question fixed on purpose has been tuned on: move it to the
tuned set and write a new one in its place. `tests/test_heldout.py` holds a
floor (12 and 9) so a change cannot quietly lose what was won; it is not a
target.

> The floor for `terms` was 13 until `answer_question` began refusing a
> question whose words name tables no one request joins. `h05-en` ("How many
> unique customers used the mobile channel?") had been read as a count of
> every customer and graded right only because all five customers used that
> channel; it is refused now, as is `h05-tr`, which was answered wrongly
> before. The tables above are the run at the commit they name and still
> say 13: they have not been regenerated.

The 13th, h06-en ("rank the segments by number of transactions"), came from
supporting transforms over a join, a gap chosen knowing that question hit it.
It stays in the set, but it is not an unseen result: the clean number is 12
of the other 15.

The three `terms` still misses: "made the most" (no limit pattern for it),
lower-case "web" (short lower-case values are not matched on purpose), and
"sırala" (not known as a ranking).

### Follow-up questions

`questions/banka-followup.json` holds 16 follow-ups (8 conversations, Turkish
and English): each has the question asked first (`after`) and the one that
follows up on its answer ("peki kanal bazında?", "only the cancelled ones",
"what about the average?", "just the top 2", "and monthly"). `python -m
bench.followup` answers the opening question with `answer_question`, passes
its request back as `previous`, and grades only the follow-up, with no model:

| | follow-ups (16) |
|---|---|
| `terms`, follow-up (`answer_question` with `previous`) | 15 |
| `terms`, after keeping stem-sharing value words | 16 |

The miss was `f08-en`, "What about standard ones?" after premium customers.
The catalog does give `standart` the word "standard", and `check_question`
read it as that value, but the planner never used it: cleaning a vocabulary
drops a word that starts with the name's stem, as an echo the stem match
already covers. That holds for a column, matched by stem; a value is matched
as a whole word (`standart\w*`), so "standard" was lost. Values now drop only
the value itself. Nothing else moved (tuned 48, held-out 13). It was fixed
knowingly, so like `h06-en` it is not an unseen result: the clean number is
15 of the other 15. The set was written before the planner was run on
it, and is held to the held-out rule: not tuned on. One question was changed
after the first run, and not to pass: `f08` first asked for a number of
transactions, which premium and standard customers happen to share (4), so a
follow-up read as nothing scored as right. It now asks for the amount, and a
test checks that no opening question's own answer matches its follow-up's
gold. `tests/test_followup.py` holds a floor of 16.

### Questions it cannot read

`questions/banka-unreadable.json` holds 16 questions (8 in each language)
answerable from the schema but asked in words the certified vocabulary does
not hold ("Ortalama harcama ne kadar?", "How many offices do we have?",
"What is the total turnover?"); a test checks `check_question` says `none`
for every one. `answer_question` used to answer them with a guess, a count
of `subeler`, right only where the question happened to ask that (2 of 16).
It now refuses them, naming the words it did not read (0 of 16, and nothing
wrong said with confidence). `python -m bench.unreadable` runs three arms
(2026-11-09, `d74abf9`, `qwen2.5-coder:7b` on the GB10; all three re-run at
`acfbb5a` with the same verdicts):

| Arm | unreadable (16) |
|---|---|
| `terms` (refused; a guess before, 2) | 0 |
| `catalog` (the model writes SQL from the annotated schema) | 11 |
| `terms+model-on-none` | 11 |
| `terms+sql-on-unread` (the product's `olap.unread_questions: sql`) | 11 |

`terms+model-on-none` is `answer_question`, except where `check_question`
says it read nothing, which goes to the `catalog` arm. No question in the
tuned or held-out set reads as `none` (a test holds that), so there it is
`terms` exactly: 48 and 13. Over all three sets that is 72 of 80 against
`terms`' 61 (63 when it still guessed), and unlike the correction step it
breaks nothing it was given, because it is given only what the vocabulary
could not read. The model's
misses: "turnover" and "ciro" (both languages), "which office spent the
most" (both), and "average spend" in English.

`terms+sql-on-unread` is the product's own path, measured at the commit
that added it: `answer_question` with a model behind it, building the schema
it shows from the catalog rather than from the bench's hand-written
annotations. It answers 11 as well, with different misses ("the biggest
spend" and "how many different people" in English, "ciro", "which office"
in both languages, one of them SQL that did not run).

Tried and not kept (2026-11-10): sending SQL that did not run back to the
model once, with the error. Still 11: the one miss that was SQL failing,
"Which office spent the most?", joins `subeler` to `islemler` on a
`sube_kodu` that `islemler` does not have, and the model wrote the same join
again after being told. The path runs through `musteriler`; the schema shows
both keys, and a 7b model does not take it.

What it changed in the product: a question that names nothing the
vocabulary knows got a confident wrong answer from `answer_question`. It is
now refused with the words not read and the ways forward, so a calling model
can write `query_metrics` or SQL itself, which is the routed arm's split done
by the caller. A follow-up is never refused this way: "what about the
average?" names nothing on its own and reads from the request it follows.

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
(`truejoin.application.olap.lexicon`) and learned three things, each a general
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

## The model now subtracts

Re-run on 2026-11-09, after the planner was taught from its misses, the
reading above no longer holds. `terms` alone answers 48 of 48 and
`catalog+join-path`, the same proposal handed to the model to correct, 43:
the model broke five correct proposals (`q15` and `q17` in both languages,
`q22` in English) and fixed none, because there was nothing left to fix. That
is in-sample, so it proves little by itself; the held-out set says the same
thing more weakly. There `terms` answers 13 of 16 and `catalog+join-path` 11:
the model broke three (`h01-tr`, `h02-tr`, `h02-en`) and fixed one (`h05-tr`).
Neither gap is significant (p = 0.06 and 0.63).

**Handing the model only the gaps does not rescue it.** `terms+gaps` sends a
question to the model only when `check_question` says a word went unread
(`part` or `none`); a question read word for word goes as `answer_question`
reads it. On the 48 every question is read word for word, so it never calls
the model and equals `terms`. On the held-out 16 five are not (`h01-tr`,
`h02-tr`, `h05-tr`, `h05-en`, `h06-tr`), and on three of them `terms` was
already right: the model broke two of those and fixed `h05-tr`, so 12 against
`terms`' 13. An unread word does not mean a wrong reading: the two it broke
left only "hangileri" ("which ones") and "say" ("count") unread, and were
answered correctly without them. Coverage says where to look,
not whether the answer is wrong.

What it changes: the copilot shape (the tool answers, a model corrects the
reading) was worth eleven answers when the vocabulary read half the
questions. Now that it reads most of them, a correction step that cannot tell
a right proposal from a wrong one costs more than it saves. The first way
out, handing the model only the proposals `check_question` marks `part` or
`none`, was measured as `terms+gaps` above and still loses one. The other,
spending the model only on the questions the planner refuses outright, cannot
be measured on these two sets: `answer_question` refused none of the 64
questions. Its three held-out misses (`h01-en`, `h05-tr`, `h06-tr`) are
readings that ran and were wrong. It was measured on a set written for it,
[below](#questions-it-cannot-read).
The gate still passes (`catalog+join-path` beats `catalog` by 18.8 points on
the 48 and 6.2 on the held-out 16, inventing nothing), so `olap.terms` stays
on.

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
| `questions/` | The question sets, as data: the tuned 48 and the held-out 16. |
| `heldout.py` | The no-model arms on both sets, side by side. |
| `lexicons/` | Generated vocabularies, checked in for review. |
