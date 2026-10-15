# DECISIONS.md  (about 150-200 words in total - plain language, your own words)



**1. Indexes:** Unique `emp\_code` for employees; unique `(emp\_code, date)` for punch-in, PATCH and monthly lookups; `(date desc, emp\_code asc)` for the attendance list; `(emp\_code, punch\_in desc)` for punch-out; department and `joined\_on` for employee queries. Rejected `status`: few distinct values, little filtering, slower writes.



**2. Punch-in race:**Both requests compute the same (emp\_code, date) and call `insert\_one`. The unique index lets one succeed; the other raises `DuplicateKeyError`, returned as 409. I never check first, so there is no gap.



**3. Ties**: $rank gives tied employees the same rank and skips the next one. The limit applies after ranking, so a tie at the cutoff returns extra rows rather than dropping someone equally late.



**4. Headcount:** The pipeline starts from `employees`, keeps those who joined by the month end, then uses `$lookup` for their logs. Someone with no logs still counts as one.



**5. 100x data:**Monthly analytics scan every log in the month on each request. I would keep a pre-aggregated summary per employee per month, updated on punch-out and each correction, so reports read one document. The trade-off is keeping it in step with PATCH edits.

