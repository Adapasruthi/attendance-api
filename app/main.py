"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, model_validator
from bson import Decimal128, json_util
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                   # times come back as UTC, not "naive"
    serverSelectionTimeoutMS=3000,   # fail fast if the database is down
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt):
    """datetime -> epoch milliseconds (None stays None)."""
    if dt is None:
        return None
    if dt.tzinfo is None:            # safety: treat naive times as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """epoch milliseconds -> UTC datetime, cut to whole seconds (rule R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    """The current time in UTC, cut to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def hhmm(s: str) -> time:
    """'09:30' -> time(9, 30)"""
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(emp) -> bool:
    """22:00-06:00 style shift: end is on or before start."""
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp) -> date:
    """R1: the IST calendar date of the punch-in. For a night shift, a punch
    earlier than shift_end belongs to the shift that started the day before."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d


def shift_bounds(emp, att_date: date):
    """Exact start and end instants of the shift that starts on att_date (IST).
    A night shift ends on the NEXT calendar day."""
    start = datetime.combine(att_date, hhmm(emp["shift_start"]), tzinfo=IST)
    end_day = att_date + timedelta(days=1) if is_overnight(emp) else att_date
    end = datetime.combine(end_day, hhmm(emp["shift_end"]), tzinfo=IST)
    return start, end


# --------------------------------------------------------------------------- #
# Business-rule helpers (R2 - R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if MORE than 10:00 after start; then whole minutes since start."""
    seconds = (punch_in - shift_start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours rounded to 2 decimals, half-up (Decimal, not float round())."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only counted if 30 or more."""
    minutes = int((punch_out - shift_end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Output helpers: shape MongoDB documents for the API
# --------------------------------------------------------------------------- #
def employee_out(doc) -> dict:
    """Shape an employee document for the API (no _id, created_at in ms)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def record_out(doc) -> dict:
    """Shape an attendance record for the API: times in ms, no _id,
    and safe defaults for old records missing half_day / history."""
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for field, ch in (h.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):     # these hold dates inside!
                changes[field] = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
            else:
                changes[field] = {"from": ch.get("from"), "to": ch.get("to")}
        history.append({
            "at": to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Validation / query helpers
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]


def invalid(loc: list, msg: str):
    """Raise a 422 in the same format FastAPI uses for its own validation errors."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_day(value: str, loc: list) -> date:
    """'YYYY-MM-DD' -> date, or 422 if it is not a real calendar date."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid(loc, "must be a date in YYYY-MM-DD format")


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Builds the MongoDB filter for GET /attendance (reused later by /admin/explain)."""
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Indexes and app startup
# --------------------------------------------------------------------------- #
def create_indexes():
    """Runs once when the app starts. Safe to run many times (idempotent)."""
    # One employee per emp_code - the DATABASE enforces it (race-safe)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # Listing / counting employees of one department
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    # At most one record per employee per day - makes punch-in race-safe
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Listing attendance sorted by date (newest first), then emp_code
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # Punch-out: "latest punch_in at or before time X" for one employee
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    # Department summary: "employees who joined on or before the end of the month"
    db.employees.create_index([("joined_on", ASCENDING)])


@asynccontextmanager
async def lifespan(app):
    create_indexes()      # runs at startup
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"      # 00:00 to 23:59

# Epoch milliseconds: a real integer (no floats, no strings), not "seconds-looking"
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")          # EMP + 4-6 digits
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: date                                         # must be a real date

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    """Only these fields can be changed. Derived fields sent by a client are ignored."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")   # ask MongoDB "are you there?"
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()   # store as "YYYY-MM-DD" string
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)                # the unique index decides
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),                # page must be 1 or more
    page_size: int = Query(20, ge=1, le=100),  # between 1 and 100
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size              # page 1 skips 0 records
    total = db.employees.count_documents(q)    # count WITH the filter
    cursor = (
        db.employees.find(q)
        .sort("emp_code", 1)                   # EMP0001, EMP0002...
        .skip(skip)
        .limit(page_size)
    )
    items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # from_ms() also cuts off the milliseconds (R1)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp)
    shift_start, _ = shift_bounds(emp, att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)   # unique (emp_code, date) index decides
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out time
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_at = rec["punch_in"]
    if ts <= punch_in_at:
        invalid(["body", "punched_at"], "punch-out must be after punch-in")
    if ts - punch_in_at > timedelta(hours=24):
        invalid(["body", "punched_at"], "punch-out more than 24 hours after punch-in")

    _, shift_end = shift_bounds(emp, date.fromisoformat(rec["date"]))
    work_hours = compute_work_hours(punch_in_at, ts)

    # Atomic: only succeeds if punch_out is STILL empty (race-safe)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(ts, shift_end),
            "half_day": work_hours < 4.5,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:   # another request closed it a moment ago
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,        # must be a real YYYY-MM-DD date
    date_to: Optional[date] = None,
    status: Optional[Status] = None,         # only the 5 valid statuses
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        invalid(["query", "date_from"], "date_from must be on or before date_to")
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)                # database sorts, using the index
        .skip((page - 1) * page_size)
        .limit(page_size)                     # only one page leaves the database
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# Fields that a correction can change, in the order they appear in history
CHANGE_FIELDS = ["status", "punch_in", "punch_out", "work_hours",
                 "late_minutes", "overtime_minutes", "half_day"]


def current_values(rec) -> dict:
    """The record's current values, with defaults for old records missing fields."""
    return {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    # NOTE: the path parameter is called "date" (the contract says so), so inside
    # this function "date" is the string from the URL, not the datetime.date type.
    day = parse_day(date, ["path", "date"])

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set              # which fields the client actually sent
    for f in ("punch_in", "punch_out"):
        if f in sent and getattr(body, f) is None:
            invalid(["body", f], "must be epoch milliseconds, not null")

    old = current_values(rec)
    new_status = body.status or old["status"]

    if new_status in ("ABSENT", "LEAVE"):
        # ABSENT / LEAVE clears both punch times, so sending one is a mistake
        if body.punch_in is not None or body.punch_out is not None:
            invalid(["body"], "ABSENT/LEAVE cannot have punch_in or punch_out")
        new = {"status": new_status, "punch_in": None, "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        pi = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        po = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pi is None:
            invalid(["body", "punch_in"], "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(pi, emp) != day:
            invalid(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if po is not None and not (pi < po <= pi + timedelta(hours=24)):
            invalid(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

        # Recompute every derived field from the FINAL times (R2 - R5)
        shift_start, shift_end = shift_bounds(emp, day)
        new = {"status": new_status, "punch_in": pi, "punch_out": po,
               "late_minutes": compute_late_minutes(pi, shift_start)}
        if po is not None:
            wh = compute_work_hours(pi, po)
            new.update(work_hours=wh, overtime_minutes=compute_overtime(po, shift_end),
                       half_day=wh < 4.5)
        else:
            new.update(work_hours=None, overtime_minutes=0, half_day=False)

    # Only fields that ACTUALLY changed go into history
    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if old[f] != new[f]}
    if not changes:
        invalid(["body"], "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])

    # Atomic update that only succeeds if nobody changed the record since we read it:
    # same times/status, and history still has exactly n entries (no entry at index n).
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            f"history.{n}": {"$exists": False},
        },
        {"$set": new, "$push": {"history": entry}},   # append, never overwrite
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_range(month: str):
    """'2026-07' -> (date(2026,7,1), date(2026,7,31)). 422 if the year is impossible."""
    try:
        y, m = map(int, month.split("-"))
        first = date(y, m, 1)
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    except (ValueError, OverflowError):
        invalid(["query", "month"], "not a valid month")
    return first, last


def count_working_days(start: date, end: date) -> int:
    """R7: Monday-Friday days from start to end, inclusive (pure calendar maths)."""
    days = 0
    d = start
    while d <= end:
        if d.weekday() < 5:          # 0 = Monday ... 4 = Friday
            days += 1
        d += timedelta(days=1)
    return days


def as_decimal(value) -> Decimal:
    """Any number coming back from MongoDB (int, float, Decimal128) -> exact Decimal."""
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round_half_up(value, places: int = 2):
    """R8: round half-up (never banker's rounding). None stays None."""
    if value is None:
        return None
    d = as_decimal(value)
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ---- Small aggregation-expression building blocks (used inside pipelines) ----
def ex_ifnull(field, default):
    return {"$ifNull": [field, default]}


# Monday=1 ... Sunday=7, computed from the "YYYY-MM-DD" string in the database
EX_WEEKDAY = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
EX_IS_PRESENT = {"$in": ["$status", PRESENCE]}
# R5/R6: a presence record is worth 1, or 0.5 if it is a half day
EX_PRESENT_VALUE = {"$cond": [EX_IS_PRESENT, {"$cond": [ex_ifnull("$half_day", False), 0.5, 1]}, 0]}
# R7: present_days only counts Monday-Friday records
EX_PRESENT_WEEKDAY = {"$cond": [{"$lte": [EX_WEEKDAY, 5]}, EX_PRESENT_VALUE, 0]}
EX_LATE = ex_ifnull("$late_minutes", 0)
EX_IS_LATE = {"$cond": [{"$gt": [EX_LATE, 0]}, 1, 0]}


def ex_count_if(condition):
    return {"$cond": [condition, 1, 0]}


# --------------------------------------------------------------------------- #
# Analytics pipelines (built in one place so /admin/explain runs the SAME thing)
# --------------------------------------------------------------------------- #
def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        # Uses the (emp_code, date) index
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": EX_PRESENT_WEEKDAY},
            "leave_days": {"$sum": ex_count_if({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": EX_IS_LATE},
            "total_late_minutes": {"$sum": EX_LATE},
            "total_overtime_minutes": {"$sum": ex_ifnull("$overtime_minutes", 0)},
        }},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    match = {"joined_on": {"$lte": last.isoformat()}}       # R9 headcount
    if department:
        match["department"] = department
    logs_in = "$logs"
    return [
        # 1. Start from EMPLOYEES, so people with zero logs are still counted
        {"$match": match},
        # 2. Attach each employee's logs for the month (uses the (emp_code, date) index)
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # 3. One row per employee with totals over their logs (empty logs -> zeros)
        {"$project": {
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": logs_in, "as": "l", "in": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$$l.status", PRESENCE]},
                        {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {
                            "dateString": "$$l.date", "format": "%Y-%m-%d"}}}, 5]},
                    ]},
                    {"$cond": [ex_ifnull("$$l.half_day", False), 0.5, 1]},
                    0,
                ]}}}},
            # average is over RECORDS: keep the sum and the count, divide at the end.
            # $toDecimal keeps the sum exact (no float drift before rounding).
            "wh_sum": {"$sum": {"$map": {"input": logs_in, "as": "l", "in": {
                "$cond": [
                    {"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$isNumber": "$$l.work_hours"}]},
                    {"$toDecimal": "$$l.work_hours"},
                    {"$toDecimal": 0},
                ]}}}},
            "wh_count": {"$sum": {"$map": {"input": logs_in, "as": "l", "in": {
                "$cond": [{"$and": [{"$in": ["$$l.status", PRESENCE]},
                                    {"$isNumber": "$$l.work_hours"}]}, 1, 0]}}}},
            "late_count": {"$sum": {"$map": {"input": logs_in, "as": "l", "in": {
                "$cond": [{"$gt": [ex_ifnull("$$l.late_minutes", 0), 0]}, 1, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": logs_in, "as": "l",
                                                     "in": ex_ifnull("$$l.late_minutes", 0)}}},
            "leave_count": {"$sum": {"$map": {"input": logs_in, "as": "l", "in": {
                "$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}}}},
            "on_duty_count": {"$sum": {"$map": {"input": logs_in, "as": "l", "in": {
                "$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}}}},
        }},
        # 4. One row per department
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_count": {"$sum": "$wh_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline = [
        # Only late records of the month (uses the (date, emp_code) index)
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        # name + department come from employees; logs of unknown employees are dropped
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        # filter BEFORE ranking, so ranks are inside the department
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, next rank skipped (1,2,2,4)
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        # limit applies AFTER ranking: keep everyone whose rank <= limit (ties included)
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, start: date, end: date) -> list:
    s, e = start.isoformat(), end.isoformat()
    start_dt = datetime.combine(start, time(0), tzinfo=UTC)
    end_excl = datetime.combine(end + timedelta(days=1), time(0), tzinfo=UTC)
    zero_row = {"date": s, "joins": 0, "present": 0, "late": 0}
    return [
        # 1. The department's employees (uses the (department, emp_code) index)
        {"$match": {"department": department}},
        # 2. Their logs inside the range (uses the (emp_code, date) index)
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": s, "$lte": e}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # 3. Turn every employee into "events" on days:
        #    - a JOIN event on max(joined_on, start) if they joined on or before `end`
        #      (a running total of joins = headcount on each day, R9)
        #    - one event per log with its present value and late flag
        #    - a zero row on `start`, so the series is never empty
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [zero_row],
            {"$cond": [
                {"$lte": ["$joined_on", e]},
                [{"date": {"$max": ["$joined_on", s]}, "joins": 1, "present": 0, "late": 0}],
                [],
            ]},
            {"$map": {"input": "$logs", "as": "l", "in": {
                "date": "$$l.date",
                "joins": 0,
                "present": {"$cond": [
                    {"$in": ["$$l.status", PRESENCE]},
                    {"$cond": [ex_ifnull("$$l.half_day", False), 0.5, 1]},
                    0,
                ]},
                "late": {"$cond": [{"$gt": [ex_ifnull("$$l.late_minutes", 0), 0]}, 1, 0]},
            }}},
        ]}}},
        {"$unwind": "$events"},
        # 4. One row per day that has something
        {"$group": {"_id": "$events.date",
                    "joins": {"$sum": "$events.joins"},
                    "present_count": {"$sum": "$events.present"},
                    "late_count": {"$sum": "$events.late"}}},
        {"$project": {"_id": 0,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                      "joins": 1, "present_count": 1, "late_count": 1}},
        # 5. Gap filling: exactly one row per calendar day in [start, end]
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
                                               "bounds": [start_dt, end_excl]}}},
        # 6. Running headcount = all joins so far
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"headcount": {"$sum": "$joins",
                                     "window": {"documents": ["unbounded", "current"]}}},
        }},
        # 7. Per-day values; rate only on working days with headcount > 0, else null
        {"$set": {
            "present_count": ex_ifnull("$present_count", 0),
            "late_count": ex_ifnull("$late_count", 0),
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            # exact decimal division, then round half-up to 4 decimals
            {"$divide": [{"$trunc": {"$add": [
                {"$multiply": [{"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 10000]},
                {"$toDecimal": "0.5"},
            ]}}, 10000]},
            None,
        ]}}},
        # 8. 7-day moving average: current row + up to 6 rows before it (inside the range).
        #    $avg skips nulls, and gives null if the window has no rate at all.
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                         "window": {"documents": [-6, "current"]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0,
                      "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                      "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def trend_range(start: date, end: date):
    """422 if the range is backwards or longer than 92 days (inclusive)."""
    if end < start:
        invalid(["query", "to"], "to must be on or after from")
    if (end - start).days + 1 > 92:
        invalid(["query", "to"], "range must not exceed 92 days")


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_range(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # R7: working days counted from joined_on for a mid-month joiner
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined), last)

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    r = rows[0] if rows else {}
    present = r.get("present_days", 0)
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present)) / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN),
                       department: Optional[str] = None):
    first, last = month_range(month)
    rows = db.employees.aggregate(department_summary_pipeline(first, last, department))
    items = []
    for r in rows:
        avg = None
        if r["wh_count"] > 0:
            avg = round_half_up(as_decimal(r["wh_sum"]) / r["wh_count"], 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN),
                     limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    first, last = month_range(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     start: date = Query(..., alias="from"),
                     end: date = Query(..., alias="to")):
    trend_range(start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, start, end)):
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(r["attendance_rate"], 4),
            "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"]


def need(value, name: str):
    """A parameter that this endpoint's explain requires -> 422 if missing."""
    if value is None or value == "":
        invalid(["query", name], f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    start: Optional[date] = Query(None, alias="from"),
    end: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Build exactly the same query / pipeline the real endpoint builds
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            invalid(["query", "date_from"], "date_from must be on or before date_to")
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            first, last = month_range(need(month, "month"))
            collection, pipeline = "attendance_logs", monthly_pipeline(need(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            first, last = month_range(need(month, "month"))
            collection, pipeline = "employees", department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = month_range(need(month, "month"))
            collection, pipeline = "attendance_logs", leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            need(department, "department")
            trend_range(need(start, "from"), need(end, "to"))
            collection, pipeline = "employees", trend_pipeline(department, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command("explain", command, verbosity="executionStats")
    # Convert BSON types (Timestamp, ObjectId, ...) into plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}