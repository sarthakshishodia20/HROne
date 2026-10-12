"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# DB / Constants
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

# EpochMillis range defined in OpenAPI: [100_000_000_000, 4_102_444_800_000]
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    # employees: unique on emp_code (enforces concurrent-safe uniqueness)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, background=True)

    # attendance: unique compound key (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        background=True,
    )
    # attendance: list query (date range + status filter + sort by date desc, emp_code asc)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        background=True,
    )
    # analytics: filter by date range within aggregations
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        background=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def half_up(value: float, decimals: int) -> float:
    """Round a float to `decimals` places using ROUND_HALF_UP."""
    fmt = "0." + "0" * decimals
    return float(Decimal(str(value)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def epoch_to_dt(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds (int) to a UTC datetime truncated to seconds."""
    return datetime.fromtimestamp(epoch_ms // 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_epoch(dt: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def dt_to_epoch_or_none(dt: Optional[datetime]) -> Optional[int]:
    return dt_to_epoch(dt) if dt is not None else None


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: determine the IST attendance date.
    For overnight shifts (shift_end <= shift_start) a punch-in whose IST time
    is *earlier than* shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    h_end, m_end = map(int, shift_end.split(":"))
    h_start, m_start = map(int, shift_start.split(":"))

    is_overnight = (h_end * 60 + m_end) <= (h_start * 60 + m_start)
    if is_overnight:
        punch_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute
        end_minutes = h_end * 60 + m_end
        if punch_minutes < end_minutes:
            # belongs to previous day's shift
            return (punch_in_ist - timedelta(days=1)).date().isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period = 10 min. Late only if strictly > 10 min after shift_start.
    late_minutes = floor minutes since shift_start (not since grace-period end).
    """
    h, m = map(int, shift_start.split(":"))
    year, month, day = map(int, date_str.split("-"))
    start_ist = datetime(year, month, day, h, m, 0, tzinfo=IST)
    diff_seconds = (punch_in_utc - start_ist).total_seconds()
    if diff_seconds <= 600:  # <= 10 minutes → not late
        return 0
    return int(diff_seconds // 60)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, date_str: str) -> int:
    """
    R3: overtime = floor minutes after shift_end, only when >= 30; else 0.
    For overnight shifts, shift_end falls on the *next* calendar day relative to date_str.
    """
    h, m = map(int, shift_end.split(":"))
    h_start_dummy = 0  # not needed here; we use shift_end only

    year, mo, day = map(int, date_str.split("-"))
    # We compare shift_end against punch_out; for an overnight shift the shift_end
    # clock time is on the *next* day, so we must pick the right calendar date.
    # Strategy: build candidate on the same day, and if punch_out > candidate + 24h
    # that's impossible; pick the smallest non-negative diff.
    candidate_same = datetime(year, mo, day, h, m, 0, tzinfo=IST)
    candidate_next = candidate_same + timedelta(days=1)

    # pick whichever candidate is closest and results in a non-negative delta
    # (punch_out is always after shift_end for OT to exist)
    best: Optional[datetime] = None
    for candidate in (candidate_same, candidate_next):
        delta = (punch_out_utc - candidate).total_seconds()
        if delta >= 0:
            if best is None or delta < (punch_out_utc - best).total_seconds():
                best = candidate

    if best is None:
        return 0
    ot_minutes = int((punch_out_utc - best).total_seconds() // 60)
    return ot_minutes if ot_minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    raw = (punch_out_utc - punch_in_utc).total_seconds() / 3600
    return half_up(raw, 2)


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def recompute_derived(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    status: str,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> dict:
    """Recompute all derived fields given the final state of a record."""
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late_minutes = compute_late_minutes(punch_in, shift_start, date_str)
    if punch_out is None:
        return {
            "late_minutes": late_minutes,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime_minutes = compute_overtime_minutes(punch_out, shift_end, date_str)
    half_day = compute_half_day(work_hours)
    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def format_record(doc: dict) -> dict:
    """Serialize a MongoDB attendance doc to the API response shape."""
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_epoch_or_none(doc.get("punch_in"))
    doc["punch_out"] = dt_to_epoch_or_none(doc.get("punch_out"))
    doc["late_minutes"] = doc.get("late_minutes", 0)
    doc["overtime_minutes"] = doc.get("overtime_minutes", 0)
    doc["half_day"] = doc.get("half_day", False)
    doc["history"] = doc.get("history", [])
    # Serialize history punch times
    for entry in doc["history"]:
        entry["at"] = dt_to_epoch(entry["at"]) if isinstance(entry["at"], datetime) else entry["at"]
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                for side in ("from", "to"):
                    v = changes[field].get(side)
                    if isinstance(v, datetime):
                        changes[field][side] = dt_to_epoch(v)
    return doc


def format_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_epoch(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

def validate_epoch(v: Optional[int]) -> Optional[int]:
    if v is None:
        return v
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("must be an integer")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(f"epoch ms must be between {EPOCH_MIN} and {EPOCH_MAX}")
    return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str  # YYYY-MM-DD

    @model_validator(mode="after")
    def shift_times_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_epoch(cls, v):
        return validate_epoch(v)

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_epoch(cls, v):
        return validate_epoch(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_epoch(cls, v):
        return validate_epoch(v)

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ---- Employees ----

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for emp in items:
        format_employee(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/employees/{emp_code}")
def get_employee(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    return format_employee(emp)


# ---- Attendance ----

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    # 404 if employee unknown
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Resolve punch-in timestamp (truncate to seconds per R1)
    now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)
    if body.punched_at is not None:
        punch_in_utc = epoch_to_dt(body.punched_at)
    else:
        punch_in_utc = now_utc

    # R1: attendance date
    date_str = attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])

    # R2: late minutes
    late_minutes = compute_late_minutes(punch_in_utc, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # Concurrency: rely on unique index (emp_code, date) — no check-then-insert
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    return format_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)
    if body.punched_at is not None:
        punch_out_utc = epoch_to_dt(body.punched_at)
    else:
        punch_out_utc = now_utc

    # Find the most recent open record with punch_in <= punch_out_utc
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # Check if there's an already-punched-out record
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punch_out_utc},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed:
            raise HTTPException(409, detail="record already punched out")
        raise HTTPException(404, detail="no open punch-in found")

    punch_in_utc: datetime = record["punch_in"]

    # 422 if punch_out <= punch_in or > 24h after punch_in
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

    date_str: str = record["date"]
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime_minutes(punch_out_utc, emp["shift_end"], date_str)
    half_day = compute_half_day(work_hours)

    update = {
        "$set": {
            "punch_out": punch_out_utc,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
    }

    # Concurrency: filter on punch_out: None so only one request succeeds
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        update,
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="record already punched out")

    return format_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {ALL_STATUSES}")

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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    for doc in docs:
        format_record(doc)
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # 404 if employee does not exist
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # 404 if no record for that date
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record for that date")

    new_status = body.status if body.status is not None else record["status"]

    # Rule: ABSENT/LEAVE + punch times → 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                detail="Cannot supply punch times when setting status to ABSENT or LEAVE",
            )

    # Resolve new punch times
    old_pi: Optional[datetime] = record.get("punch_in")
    old_po: Optional[datetime] = record.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        new_pi: Optional[datetime] = None
        new_po: Optional[datetime] = None
    else:
        new_pi = epoch_to_dt(body.punch_in) if body.punch_in is not None else old_pi
        new_po = epoch_to_dt(body.punch_out) if body.punch_out is not None else old_po

        # A presence status requires a punch_in
        if new_pi is None:
            raise HTTPException(422, detail="Presence status requires punch_in")

        # R1: punch_in must be on the attendance date (IST)
        pi_date_ist = new_pi.astimezone(IST).date().isoformat()
        if pi_date_ist != date:
            raise HTTPException(
                422,
                detail="punch_in must be on the record's attendance date (IST)",
            )

        # punch_out must be after punch_in and within 24 hours
        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    derived = recompute_derived(new_pi, new_po, new_status, emp["shift_start"], emp["shift_end"], date)

    # Build changes dict (only fields that actually changed)
    changes: dict = {}

    def maybe_record(field: str, old_val, new_val) -> None:
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    old_status = record["status"]
    maybe_record("status", old_status, new_status)

    # For punch times in changes: epoch ms (or None)
    maybe_record("punch_in", dt_to_epoch_or_none(old_pi), dt_to_epoch_or_none(new_pi))
    maybe_record("punch_out", dt_to_epoch_or_none(old_po), dt_to_epoch_or_none(new_po))
    maybe_record("work_hours", record.get("work_hours"), derived["work_hours"])
    maybe_record("late_minutes", record.get("late_minutes", 0), derived["late_minutes"])
    maybe_record("overtime_minutes", record.get("overtime_minutes", 0), derived["overtime_minutes"])
    maybe_record("half_day", record.get("half_day", False), derived["half_day"])

    if not changes:
        raise HTTPException(422, detail="Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Build the $set document, storing punch times as BSON dates
    set_doc: dict = {
        "status": new_status,
        "punch_in": new_pi,
        "punch_out": new_po,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    # Concurrency: use optimistic locking on the history array length
    history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {"$set": set_doc, "$push": {"history": history_entry}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit detected; please retry")

    return format_record(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def parse_month(month: str):
    """Return (year, month_int, first_day_str, last_day_str)."""
    year, mo = map(int, month.split("-"))
    # last day of month
    if mo == 12:
        last = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        last = datetime(year, mo + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    first_str = f"{year:04d}-{mo:02d}-01"
    last_str = last.strftime("%Y-%m-%d")
    return year, mo, first_str, last_str


def count_working_days_in_month(year: int, mo: int, joined_on: str) -> int:
    """Mon-Fri working days in [max(first_of_month, joined_on), last_of_month] (R7)."""
    from calendar import monthrange
    _, last_day = monthrange(year, mo)
    first_str = f"{year:04d}-{mo:02d}-01"
    last_str = f"{year:04d}-{mo:02d}-{last_day:02d}"
    start_str = max(first_str, joined_on)
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(last_str, "%Y-%m-%d")
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        d += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    year, mo, first_str, last_str = parse_month(month)

    # Aggregation over attendance_logs for this employee and month
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
        {
            "$group": {
                "_id": None,
                "present_days_full": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, False]},
                                    # only Mon-Fri for present_days (R7)
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "present_days_half": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = r["present_days_full"] + r["present_days_half"] * 0.5
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    working_days = count_working_days_in_month(year, mo, emp["joined_on"])

    if working_days > 0:
        attendance_pct = half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mo, first_str, last_str = parse_month(month)

    # R9: headcount = employees with joined_on <= last day of month
    emp_filter: dict = {"joined_on": {"$lte": last_str}}
    if department:
        emp_filter["department"] = department

    # Aggregate logs for the month, grouped by emp_code
    log_pipeline = [
        {"$match": {"date": {"$gte": first_str, "$lte": last_str}}},
        {
            "$group": {
                "_id": "$emp_code",
                "present_full": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, False]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "present_half": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {"$ifNull": ["$work_hours", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$cond": [{"$ne": [{"$ifNull": ["$work_hours", None]}, None]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
    ]
    log_rows = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    # Get all qualifying employees
    employees = list(db.employees.find(emp_filter, {"_id": 0}))

    # Group by department
    dept_data: dict = {}
    for emp in employees:
        dept = emp["department"]
        if dept not in dept_data:
            dept_data[dept] = {
                "department": dept,
                "headcount": 0,
                "present_days": 0.0,
                "work_hours_sum": 0.0,
                "work_hours_count": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        dd = dept_data[dept]
        dd["headcount"] += 1
        lr = log_rows.get(emp["emp_code"], {})
        dd["present_days"] += lr.get("present_full", 0) + lr.get("present_half", 0) * 0.5
        dd["work_hours_sum"] += lr.get("work_hours_sum", 0)
        dd["work_hours_count"] += lr.get("work_hours_count", 0)
        dd["late_count"] += lr.get("late_count", 0)
        dd["total_late_minutes"] += lr.get("total_late_minutes", 0)
        dd["leave_count"] += lr.get("leave_count", 0)
        dd["on_duty_count"] += lr.get("on_duty_count", 0)

    items = []
    for dd in dept_data.values():
        wh_count = dd["work_hours_count"]
        avg_wh = half_up(dd["work_hours_sum"] / wh_count, 2) if wh_count > 0 else None
        items.append({
            "department": dd["department"],
            "headcount": dd["headcount"],
            "present_days": half_up(dd["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": dd["late_count"],
            "total_late_minutes": dd["total_late_minutes"],
            "leave_count": dd["leave_count"],
            "on_duty_count": dd["on_duty_count"],
        })

    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    _, _, first_str, last_str = parse_month(month)

    # Build emp_code filter if department given
    emp_filter: dict = {}
    if department:
        emp_filter["department"] = department

    emp_map = {e["emp_code"]: e for e in db.employees.find(emp_filter, {"_id": 0})}
    emp_codes = list(emp_map.keys())

    match_filter: dict = {
        "date": {"$gte": first_str, "$lte": last_str},
        "late_minutes": {"$gt": 0},
    }
    if emp_codes:
        match_filter["emp_code"] = {"$in": emp_codes}
    elif department:
        # department given but no employees → empty
        return {"month": month, "items": []}

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Assign standard competition ranks
    items = []
    rank = 1
    i = 0
    while i < len(rows):
        # find all rows tied at this position
        current_minutes = rows[i]["total_late_minutes"]
        j = i
        while j < len(rows) and rows[j]["total_late_minutes"] == current_minutes:
            j += 1
        # all rows[i:j] share the same rank
        if rank > limit:
            break
        for row in rows[i:j]:
            ec = row["_id"]
            emp = emp_map.get(ec)
            if emp is None:
                continue
            items.append({
                "rank": rank,
                "emp_code": ec,
                "name": emp["name"],
                "department": emp["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            })
        rank = j + 1
        i = j

    # Sort: total_late_minutes desc, emp_code asc (already sorted from aggregation, but re-sort for ties)
    items.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    # 422 if to < from or range > 92 days
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, detail="Invalid date format; use YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(422, detail="'to' must be >= 'from'")
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, detail="Date range must not exceed 92 days")

    # 404 if no employees in department
    headcount_total = db.employees.count_documents({"department": department})
    if headcount_total == 0:
        raise HTTPException(404, detail="Unknown department")

    to_str = to_date
    from_str = from_date

    # Aggregation using $densify to fill gaps
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        # Look up employee to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$match": {"emp_info.department": department}},
        # Convert date string to a proper date for $densify
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "is_half_day": {"$ifNull": ["$half_day", False]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count_full": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$eq": ["$is_half_day", False]}]},
                            1,
                            0,
                        ]
                    }
                },
                "present_count_half": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$eq": ["$is_half_day", True]}]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.strptime(from_str, "%Y-%m-%d"),
                        datetime.strptime(to_str, "%Y-%m-%d") + timedelta(days=1),
                    ],
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    raw_rows = list(db.attendance_logs.aggregate(pipeline))

    # Build per-day headcount from employees (R9: joined_on <= that day)
    employees = list(db.employees.find({"department": department}, {"_id": 0, "joined_on": 1}))
    joined_dates = sorted(e["joined_on"] for e in employees)

    result_items = []
    # For moving average: track last 7 non-null rates
    rate_window: list = []

    for row in raw_rows:
        date_dt: datetime = row["_id"]
        date_str = date_dt.strftime("%Y-%m-%d")
        is_working_day = date_dt.weekday() < 5  # Mon-Fri

        # R9 headcount: employees with joined_on <= date_str
        hc = sum(1 for jd in joined_dates if jd <= date_str)

        present_count = row.get("present_count_full", 0) + row.get("present_count_half", 0) * 0.5
        late_count = row.get("late_count", 0)

        # attendance_rate: only on working days with headcount > 0
        if is_working_day and hc > 0:
            attendance_rate = half_up(present_count / hc, 4)
        else:
            attendance_rate = None

        # moving_avg_7d: mean of non-null rates in window (up to 7)
        if attendance_rate is not None:
            rate_window.append(attendance_rate)
        else:
            rate_window.append(None)
        # only keep last 7 entries
        if len(rate_window) > 7:
            rate_window.pop(0)
        non_null = [r for r in rate_window if r is not None]
        moving_avg = half_up(sum(non_null) / len(non_null), 4) if non_null else None

        result_items.append({
            "date": date_str,
            "is_working_day": is_working_day,
            "headcount": hc,
            "present_count": half_up(present_count, 2),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg,
        })

    return {"department": department, "items": result_items}


# ---------------------------------------------------------------------------
# Admin explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID_ENDPOINTS = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in VALID_ENDPOINTS:
        raise HTTPException(422, detail=f"endpoint must be one of {VALID_ENDPOINTS}")

    collection = "attendance_logs"
    explain_result = None

    if endpoint == "attendance_list":
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
        skip = (page - 1) * page_size
        explain_result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        _, _, first_str, last_str = parse_month(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        _, _, first_str, last_str = parse_month(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_str, "$lte": last_str}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": 1}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        _, _, first_str, last_str = parse_month(month)
        match_filter: dict = {
            "date": {"$gte": first_str, "$lte": last_str},
            "late_minutes": {"$gt": 0},
        }
        pipeline = [
            {"$match": match_filter},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from, and to are required for department_trend")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    # Remove BSON ObjectId / non-serializable fields from explain
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if k != "$clusterTime"}
        if isinstance(obj, list):
            return [clean(i) for i in obj]
        try:
            import json
            json.dumps(obj)
            return obj
        except TypeError:
            return str(obj)

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": clean(explain_result),
    }
