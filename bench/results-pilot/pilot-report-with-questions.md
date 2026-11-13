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

| Id | Question | Rows returned | Rows expected | Why | Read by | Words not read |
|---|---|---|---|---|---|---|
| h01-en | Which 2 customers made the most transactions? | 1 | 2 | a different number of rows | vocabulary | 0 |
| h05-tr | Kaç farklı müşteri web kanalını kullanmış? | 1 | 1 | different values | vocabulary | 1 |
| h06-tr | Kanalları toplam tutara göre sırala | 3 | 3 | different values | vocabulary | 1 |

## Refused (6)

| Id | Question | Why | Message |
|---|---|---|---|
| h02-tr | Eposta adresi olmayan müşterileri say. | a guardrail refused it | Guardrail: eposta in banka.musteriler is masked for this caller and cannot be measured, grouped by or filtered on. |
| h02-en | Number of customers without an email | a guardrail refused it | Guardrail: eposta in banka.musteriler is masked for this caller and cannot be measured, grouped by or filtered on. |
| u01-tr | Ortalama harcama ne kadar? | none of its words are in the vocabulary | Nothing in this question is in the certified vocabulary (not read: harcama, kadar). Ask with the words the terms use (search_terms finds them), or use query_metrics or query_data; a steward can add the words. |
| u02-tr | Kaç kişi var? | none of its words are in the vocabulary | Nothing in this question is in the certified vocabulary (not read: kisi). Ask with the words the terms use (search_terms finds them), or use query_metrics or query_data; a steward can add the words. |
| u05-tr | Toplam ciro nedir? | none of its words are in the vocabulary | Nothing in this question is in the certified vocabulary (not read: ciro). Ask with the words the terms use (search_terms finds them), or use query_metrics or query_data; a steward can add the words. |
| u07-tr | Hangi ofis en çok harcadı? | none of its words are in the vocabulary | Nothing in this question is in the certified vocabulary (not read: ofis, harcadi). Ask with the words the terms use (search_terms finds them), or use query_metrics or query_data; a steward can add the words. |

## Correct (11)

| Id | Question | Rows | Read by | Words not read |
|---|---|---|---|---|
| h01-tr | En fazla tutarlı 3 işlem hangileri? | 3 | vocabulary | 1 |
| h03-tr | USD ile yapılan işlemlerin toplam tutarı | 1 | vocabulary | 0 |
| h03-en | Total EUR transaction amount | 1 | vocabulary | 0 |
| h04-tr | Kanal ve durum kırılımında işlem sayısı | 4 | vocabulary | 0 |
| h04-en | Transaction count by channel and status | 4 | vocabulary | 0 |
| h05-en | How many unique customers used the mobile channel? | 1 | vocabulary | 1 |
| h06-en | Rank the segments by number of transactions | 3 | vocabulary | 0 |
| h07-tr | Şehir bazında başarılı işlem tutarı | 3 | vocabulary | 0 |
| h07-en | Average transaction amount per channel | 3 | vocabulary | 0 |
| h08-tr | Bölge ve segment bazında müşteri sayısı | 5 | vocabulary | 0 |
| h08-en | Top 2 branches by total amount | 2 | vocabulary | 0 |

---

This report holds counts and verdicts only: no row values and no SQL. It **does** hold the question text and refusal messages: keep it inside the company.
