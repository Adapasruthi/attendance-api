"""Loads TEST data with the awkward cases the business rules talk about.

WARNING: this DELETES everything in the `employees` and `attendance_logs`
collections of the database in your .env (MONGO_DB) and replaces it.

Usage:  python seed_test_data.py

Cases included (all in July 2026; 1 July 2026 is a Wednesday):
  - the 9 official sample documents
  - EMP0007: joins mid-month (2026-07-20)
  - EMP0008: Engineering, never has any log (zero-log employee)
  - EMP0009: Ops, joins in August (not in July headcount; Ops omitted from July summary)
  - EMP0010: has a WEEKEND record (Saturday 2026-07-11): counts for late, not for present_days
  - a 3-way TIE on late minutes (35) and a 2-way tie further down
  - an ON_DUTY record
  - a log for EMP0999, an employee that does not exist (must be ignored)
"""
import os
import pathlib
from datetime import datetime, timedelta, timezone

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
here = pathlib.Path(__file__).parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

employees = json_util.loads((here / "employees.json").read_text(), json_options=opts)
logs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)


def emp(code, name, dept, joined, start="09:30", end="18:30"):
    employees.append({
        "emp_code": code, "name": name, "email": f"{code.lower()}@example.com",
        "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
        "created_at": datetime.fromisoformat(joined + "T10:00:00").replace(tzinfo=IST),
    })


def log(code, day, status="PRESENT", late=0, hours=9.0, overtime=0, half=False):
    if status in ("ABSENT", "LEAVE"):
        pi = po = None
        hours = None
        late = overtime = 0
        half = False
    else:
        pi = datetime.fromisoformat(f"{day}T09:30:00").replace(tzinfo=IST) + timedelta(minutes=late)
        po = pi + timedelta(hours=hours)
    logs.append({
        "emp_code": code, "date": day, "status": status, "punch_in": pi, "punch_out": po,
        "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
        "half_day": half, "history": [],
    })


emp("EMP0007", "Mid Month Joiner", "Sales", "2026-07-20")
emp("EMP0008", "No Logs Person", "Engineering", "2026-01-01")
emp("EMP0009", "August Joiner", "Ops", "2026-08-10")
emp("EMP0010", "Weekend Worker", "Sales", "2026-02-01")

# Ties: EMP0003 already has 35 late minutes (sample). Give EMP0001 and EMP0010 35 too.
log("EMP0001", "2026-07-09", late=35, hours=8.5)
log("EMP0010", "2026-07-11", late=35, hours=6.0, overtime=60)      # Saturday!
log("EMP0010", "2026-07-13", hours=9.25)
log("EMP0006", "2026-07-09", late=20, hours=8.4)                   # rank 4
log("EMP0007", "2026-07-20", late=12, hours=9.0)                   # tie for rank 5...
log("EMP0004", "2026-07-21", late=12, hours=4.4, half=True)        # ...with EMP0004
log("EMP0007", "2026-07-21", hours=8.0)
log("EMP0007", "2026-07-22", status="WFH", hours=7.5)
log("EMP0004", "2026-07-09", status="ON_DUTY", hours=8.0)
log("EMP0006", "2026-07-10", status="LEAVE")
log("EMP0006", "2026-07-12", hours=3.0, half=True)                 # Sunday half day
log("EMP0999", "2026-07-09", late=100, hours=8.0)                  # employee does not exist

for name, docs in (("employees", employees), ("attendance_logs", logs)):
    db[name].delete_many({})
    db[name].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")
