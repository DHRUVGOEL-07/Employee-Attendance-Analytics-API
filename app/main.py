"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Part A (review) fixes are applied to the starter; see REVIEW.md. Part B/C endpoints are still TODO at the bottom.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, StrictStr, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables take priority

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes, so naive/aware mixing cannot happen.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# Index management (idempotent; runs at startup)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # One record per (emp_code, date): the unique index is what makes punch-in race-safe.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    # GET /attendance sort order (date desc, emp_code asc) with optional date range.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    # Analytics: employees filtered by joined_on (+department); logs by month with late_minutes > 0 (covers the leaderboard).
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="date_late_emp"
    )
    # Punch-out: "latest record of this employee with punch_in <= t".
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    # Same, with a status equality filter.
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Mongo may still be starting; /health reports 503 and retries index creation.
        log.exception("index creation failed at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def shift_bounds(day: date_cls, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Shift start/end instants (aware, IST) for the shift that began on attendance date `day` (end is next day if overnight)."""
    start_t, end_t = parse_hhmm(shift_start), parse_hhmm(shift_end)
    overnight = end_t <= start_t
    return (
        datetime.combine(day, start_t, tzinfo=IST),
        datetime.combine(day + timedelta(days=1 if overnight else 0), end_t, tzinfo=IST),
    )


def shift_window(punch_in: datetime, shift_start: str, shift_end: str) -> tuple[str, datetime, datetime]:
    """
    R1: returns (attendance_date 'YYYY-MM-DD', shift_start_dt, shift_end_dt), the datetimes aware in IST.
    Overnight shift (end <= start): a punch-in earlier than shift_end belongs to the shift that began the day
    before, and the shift ends on the following calendar day.
    """
    local = punch_in.astimezone(IST)
    day = local.date()
    if parse_hhmm(shift_end) <= parse_hhmm(shift_start) and local.time().replace(microsecond=0) < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    start_dt, end_dt = shift_bounds(day, shift_start, shift_end)
    return day.isoformat(), start_dt, end_dt


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    secs = int((punch_in - shift_start_dt).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up, using exact decimal arithmetic."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes from shift end to punch-out, counted only if >= 30."""
    secs = int((punch_out - shift_end_dt).total_seconds())
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: based on the already-rounded work_hours."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_HOURS


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _hist_value(key: str, v):
    return dt_to_ms(v) if key in ("punch_in", "punch_out") else v


def serialize_attendance(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        history.append(
            {
                "at": dt_to_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": _hist_value(k, c.get("from")), "to": _hist_value(k, c.get("to"))}
                    for k, c in (h.get("changes") or {}).items()
                },
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Models / validated types
# --------------------------------------------------------------------------- #
_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
_HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


def _check_date(v: str) -> str:
    try:
        date_cls.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date (YYYY-MM-DD)")
    return v


DateStr = Annotated[str, Field(pattern=_DATE_RE), AfterValidator(_check_date)]
EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: StrictStr = Field(pattern=r"^EMP\d{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=_HHMM_RE)
    shift_end: StrictStr = Field(default="18:30", pattern=_HHMM_RE)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)
    # Derived fields sent by a client are simply ignored (pydantic drops unknown keys).


MAX_SHIFT = timedelta(hours=24)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    day, start_dt, _ = shift_window(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Filter for GET /attendance; shared with /admin/explain so both build it identically."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[DateStr], Query()] = None,
    date_to: Annotated[Optional[DateStr], Query()] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    # Most recent record whose punch_in <= punched_at (null punch_in never matches a date comparison).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > MAX_SHIFT:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    _, end_dt = shift_bounds(date_cls.fromisoformat(rec["date"]), emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(pin, ts)
    fields = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, end_dt),
        "half_day": compute_half_day(work_hours),
    }
    # Atomic compare-and-set: only the one request that still sees punch_out == null wins.
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": fields})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(fields)
    return serialize_attendance(rec)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    if new_status in PRESENCE_STATUSES:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        day, _, _ = shift_window(new_in, emp["shift_start"], emp["shift_end"])
        if day != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE")
        new_in = new_out = None

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise HTTPException(422, "request changes nothing")

    start_dt, end_dt = shift_bounds(date_cls.fromisoformat(date), emp["shift_start"], emp["shift_end"])
    if new_status in PRESENCE_STATUSES:
        new_late = compute_late_minutes(new_in, start_dt)
        new_hours = compute_work_hours(new_in, new_out) if new_out is not None else None
        new_ot = compute_overtime(new_out, end_dt) if new_out is not None else 0
        new_half = compute_half_day(new_hours)
    else:
        new_late, new_hours, new_ot, new_half = 0, None, 0, False

    new_vals = {
        "status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": new_hours,
        "late_minutes": new_late, "overtime_minutes": new_ot, "half_day": new_half,
    }
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out, "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0, "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if v != old_vals[k]}
    entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if the record is still exactly as we read it (state + history length).
    n_hist = len(rec.get("history") or [])
    hist_cond = {"history": {"$size": n_hist}} if n_hist else {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    flt = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out, **hist_cond}
    res = db.attendance_logs.update_one(flt, {"$set": new_vals, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; retry")
    rec.update(new_vals)
    rec["history"] = list(rec.get("history") or []) + [entry]
    return serialize_attendance(rec)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MonthQ = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31'). Dates are IST calendar strings, compared lexicographically."""
    y, m = int(month[:4]), int(month[5:])
    try:
        last_day = calendar.monthrange(y, m)[1]
        date_cls(y, m, 1)
    except ValueError:
        raise HTTPException(422, "invalid month")
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last_day:02d}"


def round_half_up(value, places: int) -> float:
    """R8: half-up rounding done on Decimal, never on a binary float."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def count_weekdays(first: str, last: str) -> int:
    """Number of Mon-Fri days in [first, last] (R7: no holiday calendar). Pure calendar arithmetic, no data."""
    a, b = date_cls.fromisoformat(first), date_cls.fromisoformat(last)
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5) if a <= b else 0


# Reusable aggregation expressions. Presence is counted in half-units (2 = full day, 1 = half day) so every
# sum stays an exact integer; divide by 2 only when reporting.
_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
_PRESENT_UNITS = {"$cond": [_IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]}


def weekday_expr(first: str, last: str) -> dict:
    """Mon-Fri test on the `date` string: true unless it is one of the month's Saturdays/Sundays (computed here)."""
    a, b = date_cls.fromisoformat(first), date_cls.fromisoformat(last)
    weekends = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() >= 5]
    return {"$eq": [{"$in": ["$date", weekends]}, False]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    is_weekday = weekday_expr(first, last)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # uq_emp_date index
        {
            "$group": {
                "_id": None,
                # R7: only Mon-Fri records count as present days...
                "present_units": {"$sum": {"$cond": [is_weekday, _PRESENT_UNITS, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                # ...but weekend records still add to late and overtime totals.
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
                "total_late": {"$sum": _LATE},
                "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on `employees` so that people with zero logs still count (R9); logs are joined per employee."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}  # R9: joined on or before the end of the month
    if department:
        match["department"] = department
    is_weekday = weekday_expr(first, last)
    has_hours = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
    per_employee_stats = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_units": {"$sum": {"$cond": [is_weekday, _PRESENT_UNITS, 0]}},
                # Average is over records: keep an exact sum (in hundredths) and a count, divide at the very end.
                "hours_cent": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
                "total_late": {"$sum": _LATE},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": per_employee_stats,
                "as": "s",
            }
        },
        {"$addFields": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {}]}}},  # no logs -> empty stats
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    k: {"$sum": {"$ifNull": [f"$s.{k}", 0]}}
                    for k in ("present_units", "hours_cent", "hours_n", "late_count", "total_late", "leave_count", "on_duty_count")
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs whose employee does not exist are dropped here, before ranking
    ]
    if department:
        pipe.append({"$match": {"e.department": department}})  # rank inside the department only
    pipe += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff are all kept
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": "$total",
                "late_count": 1,
            }
        },
    ]
    return pipe


MAX_TREND_DAYS = 92


def department_trend_pipeline(codes: list, joined: list, d_from: str, d_to: str) -> list:
    """
    Runs on attendance_logs for the department's employees. One bucket per date that has records, then $densify
    fills every missing calendar day, so weekends/empty days appear. Rates are kept as integers in 1/10000 units
    and rounded half-up with integer arithmetic, so no float rounding creeps in.
    """
    start = datetime.combine(date_cls.fromisoformat(d_from), time.min, tzinfo=UTC)
    end = datetime.combine(date_cls.fromisoformat(d_to), time.min, tzinfo=UTC)
    win = {"documents": [-6, 0]}  # current row + up to 6 preceding rows inside the range
    is_null_rate = {"$eq": [{"$type": "$rate_i"}, "null"]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from, "$lte": d_to}}},
        {"$group": {"_id": "$date", "p2": {"$sum": _PRESENT_UNITS}, "late": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # Upper bound padded by 2 days (it is exclusive); the extra rows are trimmed by the next $match.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end + timedelta(days=2)]}}},
        {"$match": {"d": {"$lte": end}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "p2": {"$ifNull": ["$p2", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "dow": {"$dayOfWeek": "$d"},
            }
        },
        {
            "$addFields": {
                "is_wd": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                # R9: employees whose joined_on <= this day
                "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "cond": {"$lte": ["$$this", "$date"]}}}},
            }
        },
        {
            "$addFields": {
                # rate = (p2 / 2) / headcount; in 1/10000 units, half-up = floor((p2*10000 + hc) / (2*hc))
                "rate_i": {
                    "$cond": [
                        {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
                        {"$trunc": {"$divide": [{"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]}, {"$multiply": ["$headcount", 2]}]}},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "s": {"$sum": "$rate_i", "window": win},  # $sum ignores null rates
                    "c": {"$sum": {"$cond": [is_null_rate, 0, 1]}, "window": win},  # how many non-null rates
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": "$is_wd",
                "headcount": 1,
                "present_count": {"$divide": ["$p2", 2]},
                "late_count": "$late",
                "attendance_rate": {"$cond": [is_null_rate, None, {"$divide": ["$rate_i", 10000]}]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$c", 0]},
                        # mean of the window, half-up to 4 decimals: floor((2*s + c) / (2*c)) / 10000
                        {"$divide": [{"$trunc": {"$divide": [{"$add": [{"$multiply": ["$s", 2]}, "$c"]}, {"$multiply": ["$c", 2]}]}}, 10000]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def trend_inputs(department: str, d_from: str, d_to: str) -> tuple[list, list]:
    """Validates the range and returns (emp_codes, joined_on values) of the department; 404 if it has no employees."""
    a, b = date_cls.fromisoformat(d_from), date_cls.fromisoformat(d_to)
    if b < a:
        raise HTTPException(422, "to must be >= from")
    if (b - a).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    units = int(r.get("present_units", 0))
    working_days = count_weekdays(max(first, emp["joined_on"]), last)  # R7: from joined_on for mid-month joiners
    pct = None if working_days == 0 else round_half_up(Decimal(units) * 50 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": units / 2,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late", 0)),
        "total_overtime_minutes": int(r.get("total_ot", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        n = int(r["hours_n"])
        avg = None if n == 0 else round_half_up(Decimal(int(r["hours_cent"])) / n / 100, 2)
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": int(r["present_units"]) / 2,
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
):
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    for r in rows:
        r["rank"], r["total_late_minutes"], r["late_count"] = int(r["rank"]), int(r["total_late_minutes"]), int(r["late_count"])
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[DateStr, Query(alias="from")],
    date_to: Annotated[DateStr, Query(alias="to")],
):
    codes, joined = trend_inputs(department, date_from, date_to)
    rows = list(db.attendance_logs.aggregate(department_trend_pipeline(codes, joined, date_from, date_to)))
    for r in rows:
        r["headcount"], r["late_count"] = int(r["headcount"]), int(r["late_count"])
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Explain (Part C)
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[MonthQ] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[Optional[DateStr], Query()] = None,
    date_to: Annotated[Optional[DateStr], Query()] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    """Runs explain(executionStats) on the same query/pipeline the real endpoint builds, and returns it raw."""
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        inner = {
            "find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size, "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _need(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            _need(department=department, **{"from": from_, "to": to})
            codes, joined = trend_inputs(department, from_, to)
            collection, pipeline = "attendance_logs", department_trend_pipeline(codes, joined, from_, to)
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    # BSON -> plain JSON (ObjectIds, datetimes, Int64 ...)
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }