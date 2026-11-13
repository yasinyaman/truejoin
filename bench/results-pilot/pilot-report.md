# Pilot report

- **Data:** the bench's bank fixture (3 tables), not a customer's
- **Questions:** 20: bench held-out (16) and unreadable (4), never tuned on
- **As:** pilot: no row rules; e-mail and names masked
- **Catalog:** `bench/catalogs/banka.json`, its terms certified

## Result

| Correct | Refused | Wrong | Not checked | Questions |
|---|---|---|---|---|
| 11 | 6 | 3 | 0 | 20 |

**Acceptance (at most 2 wrong):** not met (3 wrong).

A refusal is the product saying it could not read or answer a question; it is not a wrong answer. A wrong answer is one whose rows differ from the expected ones.

## Wrong answers (3)

| Id | Rows returned | Rows expected | Why | Read by | Words not read |
|---|---|---|---|---|---|
| h01-en | 1 | 2 | a different number of rows | vocabulary | 0 |
| h05-tr | 1 | 1 | different values | vocabulary | 1 |
| h06-tr | 3 | 3 | different values | vocabulary | 1 |

## Refused (6)

| Id | Why |
|---|---|
| h02-tr | a guardrail refused it |
| h02-en | a guardrail refused it |
| u01-tr | none of its words are in the vocabulary |
| u02-tr | none of its words are in the vocabulary |
| u05-tr | none of its words are in the vocabulary |
| u07-tr | none of its words are in the vocabulary |

## Correct (11)

| Id | Rows | Read by | Words not read |
|---|---|---|---|
| h01-tr | 3 | vocabulary | 1 |
| h03-tr | 1 | vocabulary | 0 |
| h03-en | 1 | vocabulary | 0 |
| h04-tr | 4 | vocabulary | 0 |
| h04-en | 4 | vocabulary | 0 |
| h05-en | 1 | vocabulary | 1 |
| h06-en | 3 | vocabulary | 0 |
| h07-tr | 3 | vocabulary | 0 |
| h07-en | 3 | vocabulary | 0 |
| h08-tr | 5 | vocabulary | 0 |
| h08-en | 2 | vocabulary | 0 |

---

This report holds counts and verdicts only: no row values and no SQL. The questions appear by id only.
