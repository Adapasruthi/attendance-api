# Employee Attendance & Analytics API

An attendance API built with FastAPI and MongoDB. All the code is in `app/main.py`.

## How to run
```bash
python -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```
Set `MONGO_URI` and `MONGO_DB` as environment variables (or in a `.env` file).
The indexes are created automatically when the app starts. Open `/docs` to try the endpoints.

## How I tested it
- `python sample_seed.py` loads the 9 sample documents.
- `python seed_test_data.py` loads the samples plus extra tricky cases: ties on late minutes, a weekend record,
  an employee with no logs, someone who joins in the middle of the month, someone who joins next month,
  an ON_DUTY record and a log for an employee that does not exist. **This replaces the data in the database.**
- `python check_analytics.py` works out the expected analytics answers in plain Python, compares them with the API,
  and checks that every `/admin/explain` plan uses an index (IXSCAN, no COLLSCAN).

## Notes
- Times are saved in MongoDB as UTC dates and sent in the API as epoch milliseconds.
- Two requests at the same time: duplicate creates and punch-ins are stopped by unique indexes. Punch-out and
  corrections only update the record if it has not changed, so the second request gets 409.
- `moving_avg_7d` is the average of the rounded `attendance_rate` values shown in each row.
- The trend allows at most 92 days, counting both the `from` and `to` days.