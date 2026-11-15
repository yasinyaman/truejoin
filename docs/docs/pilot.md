---
description: "How to run a Truejoin pilot: check the answers to your own questions on your own data before relying on them."
---

# Running a pilot

A pilot answers one question: **on our own data, with our own questions, does
it give right answers, and does it refuse the ones it cannot read?** The
customer runs it on their own server. Nobody from outside sees the data, and
the report that comes out holds counts and verdicts only.

## What it measures

Each question gets one verdict:

| Verdict | Means |
|---|---|
| **correct** | The rows are the expected rows (by value and position in the row, not by column name; in any order unless the question asks for one). |
| **refused** | It said it could not read or answer the question. This is not a wrong answer. |
| **wrong** | It answered, and the rows differ from the expected ones. |
| **not checked** | The expected query did not run, or the answer has more rows than a tool returns (100). |

The pilot passes when at most two of twenty answers are wrong
(`--max-wrong`). Refusals do not count against it: a refusal is a question the
vocabulary does not know yet, which the steward can teach it (see
[Deployment](deployment.md), the usage summary).

## Steps

**1. Install and connect** as in [Deployment](deployment.md). `olap.terms`
must be `on` (the default).

**2. Review the catalog and certify its terms.** Questions in words are read
only through certified terms:

```bash
truejoin catalog -c config/database.yaml analyze -d banka
truejoin catalog -c config/database.yaml review  -d banka
truejoin catalog -c config/database.yaml approve -d banka --certify-terms
```

Check the labels the review gave the personal columns (`email`, `name`,
`phone`, `national_id`, `tc_no`): the masks key on them.

**3. Make a pilot key** with the masks and row rules a real user would have.
The pilot then sees what that user sees, and so does its expected answer:

```yaml
settings:
  auth:
    enabled: true
    api_keys:
      - key: ${file:/run/secrets/truejoin/pilot-key}
        name: pilot
        permissions: [query]
        # row_filters: {musteriler: [{column: sube_kodu, value: IST01}]}
  masking:
    enabled: true
    rules: {email: redact, name: partial, phone: last4, national_id: last4, tc_no: last4}
```

**4. Write the questions.** Twenty questions people in the company really ask,
each with the right answer, in a file like
`examples/pilot/questions.yaml` in the repository.
Write the expected answers before the first run, and do not change them after.

**5. Run it:**

```bash
truejoin pilot run questions.yaml -c config/database.yaml --as pilot --out pilot-report.md
```

It prints `… correct, … refused, … wrong, … not checked` and writes the report:
wrong answers first, then refusals, then the rest. Every question runs through
the same path any caller's does, so each is in the audit trail.

**6. Send the report.** `pilot-report.md` names the questions by id only and
holds no row values and no SQL, so it can leave the company. For your own
reading, `--with-questions` adds the question text and the refusal messages;
that copy stays inside.

## Showing two users the same question

```bash
truejoin pilot compare "Şube bazında toplam işlem tutarı" -c config/database.yaml --as manager --as branch_ist
```

asks one question as each key and prints the answers one after the other:
the rows each one gets under its own row rules and masks. This output **does**
hold row values; it is for a screen in the room, not for sending.
