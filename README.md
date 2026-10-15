\# Employee Attendance \& Analytics API



FastAPI + MongoDB. All code is in `app/main.py`.



\## Run

&#x20;   pip install -r requirements.txt

&#x20;   set MONGO\_URI and MONGO\_DB as environment variables (or in a local .env)

&#x20;   uvicorn app.main:app --port 8000



Indexes are created at startup.



\## Notes

\- The trend endpoint reads "range exceeds 92 days" as inclusive: 92 days is allowed, 93 gives 422.

\- Tested by hand with curl on the small sample data: punch-in, punch-out, PATCH, employee monthly, department summary, late leaderboard, department trend and the explain endpoint (IXSCAN, no COLLSCAN on the sample). Time and rounding helpers were checked with assert tests.

\- Checked the punch-in and punch-out races with 10 parallel requests each (one success, the rest 409).

\- Not load-tested with 100,000 records. The PATCH concurrency check was not tested in parallel.

