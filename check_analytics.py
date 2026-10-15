"""Checks the analytics endpoints against a simple, separate Python calculation.

It reads the raw documents straight from MongoDB (same .env as the app), works out
what each answer SHOULD be with plain Python, calls your running API, and compares.

Usage (with the app running):  python check_analytics.py
Optional:                      python check_analytics.py http://127.0.0.1:8001  (other port)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

EMPS = {e["emp_code"]: e for e in db.employees.find({}, {"_id": 0})}
LOGS = list(db.attendance_logs.find({}, {"_id": 0}))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
failures = 0


def r(value, places=2):
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def api(path, **params):
    url = BASE + urllib.parse.quote(path) + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(name, got, expected):
    global failures
    if got == expected:
        print(f"  OK    {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}\n        expected: {expected}\n        got:      {got}")


def month_days(month):
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return first, last


def weekday(d):  # "YYYY-MM-DD" -> True if Mon-Fri
    return date.fromisoformat(d).weekday() < 5


def present_value(l):
    if l["status"] not in PRESENCE:
        return 0
    return 0.5 if l.get("half_day") else 1


def logs_between(first, last, codes=None):
    return [l for l in LOGS if first.isoformat() <= l["date"] <= last.isoformat()
            and (codes is None or l["emp_code"] in codes)]


# ---------------------------------------------------------------- monthly ----
def expected_monthly(code, month):
    first, last = month_days(month)
    joined = date.fromisoformat(EMPS[code]["joined_on"])
    wd, d = 0, max(first, joined)
    while d <= last:
        wd += d.weekday() < 5
        d += timedelta(days=1)
    ls = logs_between(first, last, {code})
    present = sum(present_value(l) for l in ls if weekday(l["date"]))
    return {
        "emp_code": code, "month": month, "working_days": wd,
        "present_days": r(present), "leave_days": sum(l["status"] == "LEAVE" for l in ls),
        "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in ls),
        "attendance_pct": r(Decimal(str(present)) / wd * 100) if wd else None,
    }


# ---------------------------------------------------------------- summary ----
def expected_summary(month, department=None):
    first, last = month_days(month)
    depts = {}
    for code, e in EMPS.items():
        if e["joined_on"] > last.isoformat() or (department and e["department"] != department):
            continue
        depts.setdefault(e["department"], []).append(code)
    items = []
    for dept in sorted(depts):
        ls = logs_between(first, last, set(depts[dept]))
        wh = [Decimal(str(l["work_hours"])) for l in ls
              if l["status"] in PRESENCE and isinstance(l.get("work_hours"), (int, float))]
        items.append({
            "department": dept, "headcount": len(depts[dept]),
            "present_days": r(sum(present_value(l) for l in ls if weekday(l["date"]))),
            "avg_work_hours": r(sum(wh) / len(wh)) if wh else None,
            "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
            "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
            "leave_count": sum(l["status"] == "LEAVE" for l in ls),
            "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls),
        })
    return {"month": month, "items": items}


# ------------------------------------------------------------- leaderboard ----
def expected_leaderboard(month, limit=10, department=None):
    first, last = month_days(month)
    totals = {}
    for l in logs_between(first, last):
        late = l.get("late_minutes") or 0
        if late > 0 and l["emp_code"] in EMPS:
            t = totals.setdefault(l["emp_code"], [0, 0])
            t[0] += late
            t[1] += 1
    rows = [(c, t) for c, t in totals.items()
            if not department or EMPS[c]["department"] == department]
    rows.sort(key=lambda x: (-x[1][0], x[0]))
    items = []
    for code, (total, count) in rows:
        rank = 1 + sum(1 for _, (t2, _) in rows if t2 > total)     # competition rank
        if rank <= limit:
            items.append({"rank": rank, "emp_code": code, "name": EMPS[code]["name"],
                          "department": EMPS[code]["department"],
                          "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": items}


# ------------------------------------------------------------------ trend ----
def expected_trend(dept, start, end):
    codes = {c for c, e in EMPS.items() if e["department"] == dept}
    items, rates = [], []
    d = date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        ds = d.isoformat()
        head = sum(1 for c in codes if EMPS[c]["joined_on"] <= ds)
        ls = [l for l in LOGS if l["date"] == ds and l["emp_code"] in codes]
        present = sum(present_value(l) for l in ls)
        working = d.weekday() < 5
        rate = r(Decimal(str(present)) / head, 4) if working and head > 0 else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        mavg = r(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None
        items.append({"date": ds, "is_working_day": working, "headcount": head,
                      "present_count": r(present), "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
                      "attendance_rate": rate, "moving_avg_7d": mavg})
        d += timedelta(days=1)
    return {"department": dept, "items": items}


def normalize(obj):
    """Make 3 and 3.0 compare equal (JSON numbers)."""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return float(obj)
    return obj


MONTH = "2026-07"
print(f"Checking against {BASE} with {len(EMPS)} employees and {len(LOGS)} logs\n")

print("Monthly summary")
for code in sorted(EMPS):
    st, body = api(f"/analytics/employees/{code}/monthly", month=MONTH)
    check(f"{code} {MONTH} (status {st})", normalize(body), normalize(expected_monthly(code, MONTH)))
st, _ = api("/analytics/employees/EMP9999/monthly", month=MONTH)
check("unknown employee -> 404", st, 404)
st, _ = api("/analytics/employees/EMP0001/monthly", month="2026-13")
check("bad month -> 422", st, 422)

print("\nDepartment summary")
for dept in [None] + sorted({e["department"] for e in EMPS.values()}):
    params = {"month": MONTH, **({"department": dept} if dept else {})}
    st, body = api("/analytics/departments/summary", **params)
    check(f"department={dept} (status {st})", normalize(body), normalize(expected_summary(MONTH, dept)))

print("\nLate leaderboard")
for limit in (1, 2, 3, 4, 5, 10):
    st, body = api("/analytics/leaderboard/late", month=MONTH, limit=limit)
    check(f"limit={limit} (status {st})", normalize(body), normalize(expected_leaderboard(MONTH, limit)))
for dept in sorted({e["department"] for e in EMPS.values()}):
    st, body = api("/analytics/leaderboard/late", month=MONTH, limit=1, department=dept)
    check(f"limit=1 department={dept} (status {st})", normalize(body), normalize(expected_leaderboard(MONTH, 1, dept)))

print("\nDepartment trend")
for dept in sorted({e["department"] for e in EMPS.values()}):
    for start, end in (("2026-07-01", "2026-07-31"), ("2026-07-10", "2026-07-14"), ("2026-07-06", "2026-07-06")):
        st, body = api(f"/analytics/departments/{dept}/trend", **{"from": start, "to": end})
        check(f"{dept} {start}..{end} (status {st})", normalize(body), normalize(expected_trend(dept, start, end)))
st, _ = api("/analytics/departments/Nowhere/trend", **{"from": "2026-07-01", "to": "2026-07-31"})
check("unknown department -> 404", st, 404)
st, _ = api("/analytics/departments/Sales/trend", **{"from": "2026-07-31", "to": "2026-07-01"})
check("to before from -> 422", st, 422)
st, _ = api("/analytics/departments/Sales/trend", **{"from": "2026-01-01", "to": "2026-07-01"})
check("range > 92 days -> 422", st, 422)

print("\nExplain (IXSCAN present, no COLLSCAN)")
for ep, params in [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": MONTH}),
    ("department_summary", {"month": MONTH}),
    ("late_leaderboard", {"month": MONTH}),
    ("department_trend", {"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"}),
]:
    st, body = api(f"/admin/explain/{ep}", **params)
    text = json.dumps(body)
    check(f"{ep} {params} (status {st})", (st, "IXSCAN" in text, "COLLSCAN" in text), (200, True, False))
st, _ = api("/admin/explain/employee_monthly", month=MONTH)
check("explain missing emp_code -> 422", st, 422)

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")