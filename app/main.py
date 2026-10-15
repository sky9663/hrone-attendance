"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000"""
import os
import json
from bson import json_util
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override real environment variables, as the contract requires

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
# StrictInt rejects floats, strings and bools; the range rejects values that look like seconds
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
AnyStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent: fixed names, same keys every time)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_dept_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_emp_punchin")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_joined_on")


@asynccontextmanager
async def lifespan(_app):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)  # whole seconds (R1)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Datetime from Mongo -> epoch ms. A naive datetime is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return dt_to_ms(dt) if dt else None


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def parse_date(s: str) -> date:
    """Strict YYYY-MM-DD; raises 422 otherwise."""
    try:
        if len(s) != 10:
            raise ValueError
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"invalid date: {s}")


def round_half_up(x: Decimal, places: int = 2) -> float:
    """Python's round() is banker's rounding; the contract wants half-up."""
    return float(x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of the punch-in; overnight shifts may belong to the previous day."""
    local = punch_in.astimezone(IST)
    start, end = parse_hhmm(shift_start), parse_hhmm(shift_end)
    d = local.date()
    if end <= start and local.time() < end:
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """Start and end (IST) of the shift that belongs to an attendance date."""
    d = date.fromisoformat(date_str)
    start = datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST)
    if end <= start:  # overnight: the shift ends on the next calendar day
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, date_str, shift_start, shift_end) -> int:
    """R2: late only if strictly more than 600 s after shift start; then floor minutes since start."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    secs = int((punch_in - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in, punch_out) -> float:
    """R4: seconds / 3600, half-up to 2 decimals. Decimal avoids float error."""
    secs = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(secs) / Decimal(3600))


def compute_overtime(punch_out, date_str, shift_start, shift_end) -> int:
    """R3: floor minutes after shift end, counted only if >= 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


# --------------------------------------------------------------------------- #
# Models (validation)
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v):
        date.fromisoformat(v)  # ValueError becomes a 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


# --------------------------------------------------------------------------- #
# Output shaping (no _id, instants as epoch ms, defaults for legacy records)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def history_out(h: dict) -> dict:
    changes = {}
    for key, val in h.get("changes", {}).items():
        if key in ("punch_in", "punch_out"):
            val = {"from": ms_or_none(val.get("from")), "to": ms_or_none(val.get("to"))}
        changes[key] = val
    return {"at": dt_to_ms(h["at"]), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [history_out(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)  # total reflects the filter (R10)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from  # YYYY-MM-DD strings sort correctly
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[AnyStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)  # matches ix_date_emp  # matches ix_date_emp
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before punched_at (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    secs = int((ts - rec["punch_in"]).total_seconds())
    if secs <= 0 or secs > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(rec["punch_in"], ts)
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
        "half_day": work_hours < 4.5,  # R5: uses the rounded value
    }
    # Atomic: the filter only matches while punch_out is still null,
    # so of two simultaneous requests exactly one updates the record.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Regularize (manual correction with audit trail)
# --------------------------------------------------------------------------- #
class RegularizeIn(BaseModel):
    status: Optional[AnyStatus] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply ignored (pydantic drops unknown keys)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in, new_out = None, None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            secs = int((new_out - new_in).total_seconds())
            if secs <= 0 or secs > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # recompute derived fields from the final times (R2-R5)
    if new_in is not None:
        late = compute_late_minutes(new_in, date, emp["shift_start"], emp["shift_end"])
    else:
        late = 0
    if new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)
        overtime = compute_overtime(new_out, date, emp["shift_start"], emp["shift_end"])
        half_day = work_hours < 4.5
    else:
        work_hours, overtime, half_day = None, 0, False

    new_vals = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    old_vals = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if old_vals[k] != new_vals[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: every successful correction appends one history entry,
    # so the history length works as a version number. If someone else edited the record
    # after we read it, the length differs, nothing matches, and the loser gets 409.
    n = len(rec.get("history", []))
    version_filter = {"history": {"$size": 0}} if n == 0 else {"history": {"$size": n}}
    if n == 0:  # legacy records may not have the field at all
        version_filter = {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], **version_filter},
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: shared pieces
# --------------------------------------------------------------------------- #
PRESENT = ["PRESENT", "WFH", "ON_DUTY"]
MONTH_Q = r"^\d{4}-(0[1-9]|1[0-2])$"

# Mongo's $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6
DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
IS_WEEKDAY = {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}
IS_PRESENT = {"$in": ["$status", PRESENT]}
# Present value in HALVES (2 = full day, 1 = half day) so sums stay exact integers
HALVES = {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}


def month_range(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
    except ValueError:
        raise HTTPException(422, "invalid month")
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def count_working_days(start: date, end: date) -> int:
    """Mon-Fri days from start to end inclusive (R7)."""
    if start > end:
        return 0
    total = (end - start).days + 1
    n = (total // 7) * 5
    for i in range(total % 7):
        if (start + timedelta(days=i)).weekday() < 5:
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Employee monthly
# --------------------------------------------------------------------------- #
def monthly_pipeline(emp_code: str, first: str, last: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$set": {"_dow": DOW}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, HALVES, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late": {"$sum": LATE_MIN},
            "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_Q)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    r = rows[0] if rows else {}
    present_days = r.get("present_halves", 0) / 2
    # R7: working days are counted from joined_on for mid-month joiners
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working_days = count_working_days(start, date.fromisoformat(last))
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late", 0),
        "total_overtime_minutes": r.get("total_ot", 0),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Department summary (starts from employees so people with zero logs still count)
# --------------------------------------------------------------------------- #
def dept_summary_pipeline(first: str, last: str, department: Optional[str]):
    emp_match = {"joined_on": {"$lte": last}}  # R9: joined on or before the month end
    if department:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {"$sort": {"department": 1}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$set": {"_dow": DOW}},
                {"$project": {
                    "_id": 0,
                    "ph": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, HALVES, 0]},
                    "late": IS_LATE,
                    "lm": LATE_MIN,
                    "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                    "od": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                    "whn": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]},
                    # work_hours in whole hundredths so the average can be computed exactly
                    "whc": {"$cond": [
                        {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
                        {"$toInt": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}},
                        0,
                    ]},
                }},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": {"$sum": {"$sum": "$logs.ph"}},
            "wh_cents": {"$sum": {"$sum": "$logs.whc"}},
            "wh_n": {"$sum": {"$sum": "$logs.whn"}},
            "late_count": {"$sum": {"$sum": "$logs.late"}},
            "total_late": {"$sum": {"$sum": "$logs.lm"}},
            "leave_count": {"$sum": {"$sum": "$logs.leave"}},
            "on_duty_count": {"$sum": {"$sum": "$logs.od"}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_Q), department: Optional[str] = None):
    first, last = month_range(month)
    items = []
    for r in db.employees.aggregate(dept_summary_pipeline(first, last, department)):
        avg = None
        if r["wh_n"] > 0:  # mean over records, not per-employee averages
            avg = round_half_up(Decimal(r["wh_cents"]) / Decimal(100 * r["wh_n"]))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_halves"] / 2,
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Late leaderboard (standard competition ranking via $rank)
# --------------------------------------------------------------------------- #
def leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]):
    p = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        p.append({"$match": {"emp.department": department}})  # rank inside the department only
    p += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties can exceed it
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
            "department": "$emp.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return p


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_Q),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_range(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Department trend ($densify fills the gaps, $setWindowFields does the moving average)
# --------------------------------------------------------------------------- #
def trend_pipeline(department: str, frm: str, to: str):
    d_from, d_to = parse_date(frm), parse_date(to)
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    end_excl = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)
    win = {"documents": [-6, 0]}  # current row + up to 6 earlier rows inside the range
    return [
        {"$match": {"date": {"$gte": frm, "$lte": to}, "emp_code": {"$in": codes}}},
        {"$group": {
            "_id": "$date",
            "p2": {"$sum": {"$cond": [IS_PRESENT, HALVES, 0]}},  # weekend records count too
            "late_count": {"$sum": IS_LATE},
        }},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "p2": {"$ifNull": ["$p2", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}, "_dow": {"$dayOfWeek": "$d"}}},
        {"$set": {"is_working_day": IS_WEEKDAY}},
        # rate in 1/10000ths, rounded half-up with exact integer maths
        {"$set": {"rate_scaled": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                {"$multiply": ["$headcount", 2]},
            ]}},
            None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "s7": {"$sum": "$rate_scaled", "window": win},
            "c7": {"$sum": {"$cond": [{"$ne": ["$rate_scaled", None]}, 1, 0]}, "window": win},
        }}},
        {"$set": {
            "attendance_rate": {"$cond": [{"$eq": ["$rate_scaled", None]}, None, {"$divide": ["$rate_scaled", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$c7", 0]}, None, {"$divide": [
                {"$floor": {"$divide": [
                    {"$add": [{"$multiply": ["$s7", 2]}, "$c7"]},
                    {"$multiply": ["$c7", 2]},
                ]}},
                10000,
            ]}]},
        }},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=DATE_RE),
    date_to: str = Query(..., alias="to", pattern=DATE_RE),
):
    pipeline = trend_pipeline(department, date_from, date_to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


# --------------------------------------------------------------------------- #
# Explain: runs executionStats on the SAME query/pipeline each endpoint builds
# --------------------------------------------------------------------------- #
def require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing parameters: {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_Q),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[AnyStatus] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        inner = {
            "find": collection,
            "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            first, last = month_range(month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            require(month=month)
            first, last = month_range(month)
            collection, pipeline = "employees", dept_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            first, last = month_range(month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            require(department=department, **{"from": from_, "to": to})
            collection, pipeline = "attendance_logs", trend_pipeline(department, from_, to)
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": inner, "verbosity": "executionStats"})
    # the explain output can hold datetimes/ObjectIds; json_util makes it plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}