# REVIEW.md

These are the problems I found in the starter `app/main.py`, how I noticed each one, and what I changed.

| # | Where | What was wrong | How I noticed it | How I fixed it |
|---|---|---|---|---|
| 1 | `list_employees` (`skip = page * page_size`) | Page 1 skipped the first 20 employees | `GET /employees?page=1` gave an empty list but `total` was 6 | Changed to `skip = (page - 1) * page_size` |
| 2 | `list_employees` (`count_documents({})`) | `total` counted everyone and ignored the department filter | `?department=Sales` still showed `total: 6` | Count with the same filter `q` |
| 3 | `list_employees` | No sorting, but the contract says sort by `emp_code` | Order was not EMP0001, EMP0002... | Added `.sort("emp_code", 1)` |
| 4 | `list_employees` and `list_attendance` | `page` and `page_size` had no limits (R10) | `page=0` and `page_size=5000` were accepted | `Query(ge=1)` and `Query(le=100)`, so bad values give 422 |
| 5 | `health()` | Always said "ok" without checking the database | Turned off Wi-Fi and it still returned 200 | Ping MongoDB, return 503 if it fails, 3 second timeout |
| 6 | `MongoClient(...)` | Not `tz_aware`, so times came back without a time zone | Times could shift if the server is not in UTC | Added `tz_aware=True` |
| 7 | `create_employee` (`datetime.now()`) | Used the computer's local time with no time zone | `created_at` would be off by 5:30 on an IST machine | Use UTC time with seconds only (`now_utc()`) |
| 8 | All responses with times | Times were sent as text dates, not epoch milliseconds | `created_at` showed `"2026-01-05T04:30:00"` | Added `to_ms()` and the `employee_out()` / `record_out()` helpers (also for times inside `history`) |
| 9 | `punch_in`, `list_attendance` | The internal MongoDB `_id` was returned as `"id"` | Response had `"id": "66f..."` | Removed it. Records are found by `emp_code` + `date` |
| 10 | `list_attendance` | Old records without `half_day` / `history` were returned without those fields | The EMP0002 record on 2026-07-14 had no `half_day` | `record_out()` fills in `false` and `[]` |
| 11 | `create_employee` | Checked for a duplicate first and then inserted, so two requests at the same time could both get in | Two identical POSTs at once both gave 201 | Unique index on `emp_code`. If MongoDB rejects the duplicate, return 409 |
| 12 | Whole app | No indexes were created | Queries would do a full collection scan | `create_indexes()` runs once at startup and is safe to run again |
| 13 | `EmployeeIn` | No input checks (emp_code format, email, shift times, joined_on, same start and end) | `emp_code: "hello"` or shift 09:00-09:00 gave 201 | Added patterns and a check, so bad input gives 422 |
| 14 | `compute_late_minutes` | Did not use IST, and the 10 minute grace check used minutes after cutting the seconds | Shift 09:30, punch 09:40:30 gave 0 but should be 10 | Compare with the IST shift start in seconds: more than 600 seconds is late, then whole minutes since start |
| 15 | `compute_work_hours` | `round()` is not half-up rounding (R4) | Values ending exactly in 5 can round down | Use `Decimal` with `ROUND_HALF_UP` |
| 16 | `compute_overtime` | No 30 minute minimum (R3), and night shifts that end the next day were not handled | 5 minutes extra gave overtime 5 | Only count 30 minutes or more; `shift_bounds()` moves the night shift end to the next day |
| 17 | `punch_in` | Unknown employee crashed with 500 because `emp` was `None` | Punch-in for EMP9999 gave 500 | Return 404 |
| 18 | `punch_in` (`fromtimestamp`) | The computer's time zone decided the date, milliseconds were kept, and night shifts were ignored (R1) | Night shift punch at 01:30 IST got the wrong date | `from_ms()` (UTC, seconds only) and `attendance_date()` for the IST/night shift rule |
| 19 | `PunchInIn` | Any status was accepted, and `punched_at` accepted seconds, decimals and text | `punched_at: 1784520600` was accepted | Only PRESENT/WFH/ON_DUTY allowed; `punched_at` must be a whole number in milliseconds, otherwise 422 |
| 20 | `punch_in` | Same check-then-insert problem as #11 | Two punch-ins at once could make two records | Unique index on `emp_code` + `date`; duplicate gives 409 |
| 21 | `list_attendance` | Loaded every record into Python and sorted there | Very slow with 100,000 records | MongoDB does the sort, skip and limit, using an index |
| 22 | `list_attendance` | Sorted by `date` only | Records on the same date came in random order | Sort by `date` newest first, then `emp_code` |
| 23 | `list_attendance` | No checks on dates or status | `?status=HELLO` gave 200 with an empty list | Real date types, fixed status list, 422 if `date_from` is after `date_to` |

## Things I checked that were not bugs
- `load_dotenv()` does not overwrite variables that are already set, so real environment variables win over `.env`, which is what the task wants.
- Removing `_id` after `insert_one` in `create_employee` was fine (I replaced that part anyway).
- In `compute_work_hours`, `punch_out - punch_in` is the right calculation. Only the rounding was wrong.