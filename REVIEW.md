# REVIEW.md
| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | health() | Always returns ok without pinging MongoDB | Stop Mongo, call /health: still 200 | Ping the database, return 503 on failure |
| 2 | MongoClient | Not tz\_aware, so datetimes come back naive | Compare stored and returned punch times | tz\_aware=True, plus explicit UTC/IST conversion helpers |
| 3 | compute\_late\_minutes | Builds shift start in the punch-in's own zone, not IST; ignores overnight shifts | UTC punch-in 04:35 vs shift 09:30 IST | Shift bounds computed in IST from the attendance date |
| 4 | compute\_late\_minutes | int() then "> 10" gives 0 for 09:40:01; the rule is strictly more than 600 s | 09:40:01 returns 0, should be 10 | Compare whole seconds > 600, then floor to minutes |
| 5 | compute\_work\_hours | round() is banker's rounding on floats; no truncation to whole seconds | A duration landing exactly on .xx5 | Decimal with ROUND\_HALF\_UP on integer seconds |
| 6 | compute\_overtime | No 30-minute minimum; same-day end only; naive vs aware datetime TypeError | Punch-out at 18:35, or next day for an overnight shift | Uses shift\_bounds, floor minutes, only if >= 30 |
| 7 | create\_employee | find\_one then insert\_one is a race, no unique index | 10 parallel POSTs create duplicates | Unique index on emp\_code, catch DuplicateKeyError for 409 |
| 8 | create\_employee | created\_at is local naive time and returned as a datetime | Look at the response | UTC now, returned as epoch ms |
| 9 | Models | No validation (emp\_code pattern, email, shifts, joined\_on, status, punched\_at) | status "LEAVE", punched\_at in seconds | Field patterns, StrictInt with range, Literal statuses, shifts must differ |
| 10 | list\_employees | skip = page \* page\_size, total ignores the filter, no sort, page=0 crashes | /employees?department=Sales\&page=1 returns no items | (page-1) skip, filtered count, sort by emp\_code, Query bounds |
| 11 | punch\_in | Unknown employee gives a 500 (None subscript) | Use an unknown emp\_code | Return 404 |
| 12 | punch\_in | Server-local naive time; date not in IST; no overnight rule; no second truncation | Punch in at 21:55 IST on an overnight shift | attendance\_date() per R1, timestamps cut to seconds |
| 13 | punch\_in | Check-then-insert race; leaks id; datetimes not epoch ms | Parallel punch-ins | Unique (emp\_code, date) index and DuplicateKeyError; record\_out() shapes output |
| 14 | list\_attendance | Loads the whole collection, sorts and paginates in Python, no tie-break, no indexes | \~100k records, or explain showing COLLSCAN | Database-side sort/skip/limit with index (date desc, emp\_code asc) |
| 15 | list\_attendance | No date\_from > date\_to check; legacy records without history/half\_day; no epoch ms | Query legacy record EMP0002 | 422 check, .get() defaults, record\_out() |
| 16 | Startup | No indexes created | Explain shows COLLSCAN | ensure\_indexes() in the lifespan hook |
\## Looked at and fine
\- load\_dotenv() does not override real environment variables, which is what the contract wants.
\- String $gte/$lte on YYYY-MM-DD dates is correct because the strings sort lexicographically.
\- The {"\_id": 0} projection in list\_employees was fine.
