"""
Employee Attendance & Analytics API
===================================

What this file is
-----------------
The whole backend for the HROne attendance assignment, written with FastAPI and
MongoDB. The assignment asks for all code in this one file, so it is split into
clearly marked sections instead of separate modules.

What it does, in simple words
-----------------------------
* Keeps a list of employees (create them, list them).
* Lets an employee punch in and punch out, and works out how late they were,
  how long they worked, their overtime and whether it was a half day.
* Lets HR correct ("regularize") one day's record and keeps an audit trail
  (`history`) of every correction.
* Answers analytics questions inside MongoDB with aggregation pipelines:
  a monthly summary per employee, a department summary, a late-comers
  leaderboard and a daily department trend with a 7-day moving average.
* Shows MongoDB `explain` output to prove the main queries use indexes.

How the file is organised (top to bottom)
-----------------------------------------
1. Configuration      - read MONGO_URI / MONGO_DB, connect to MongoDB
2. Constants & types  - status values and validated input formats
3. Time helpers       - epoch milliseconds <-> datetimes, IST
4. Business rules     - R1-R5 as small pure functions (no database)
5. Converters         - MongoDB document -> API JSON
6. Request models     - what a client is allowed to send
7. Query builders     - one function per main query, shared by the endpoint
                        and by /admin/explain, so both run the same thing
8. Indexes & app      - indexes created at startup, FastAPI app object
9. Endpoints          - thin HTTP handlers that glue the pieces together

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


# =========================================================================== #
# 1. Configuration
# =========================================================================== #
load_dotenv()  # fills in values from .env only when they are NOT already set

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("Set MONGO_URI and MONGO_DB (environment variables or a local .env file).")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]


# =========================================================================== #
# 2. Constants and validated input types
# =========================================================================== #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = 4.5       # R5
MAX_SHIFT = timedelta(hours=24)  # longest allowed punch-in -> punch-out
MAX_TREND_DAYS = 92


class Status(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"


class PresenceStatus(str, Enum):
    """The statuses that count as present (R6) and the only ones punch-in accepts."""
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


PRESENCE = [s.value for s in PresenceStatus]


class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def _real_date(value: str) -> str:
    date.fromisoformat(value)  # the pattern allows 2026-02-30; this rejects it
    return value


def _real_month(value: str) -> str:
    date.fromisoformat(f"{value}-01")  # rejects year 0000
    return value


# [0-9] instead of \d: Pydantic's regex engine lets \d match non-ASCII digits.
IsoDate = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_real_date)]
Month = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_real_month)]
ShiftTime = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict: floats, strings and booleans are rejected, not coerced. Bounds reject "seconds".
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def invalid(field: str, message: str, where: str = "body") -> RequestValidationError:
    """A 422 error in FastAPI's standard shape, for rules a schema cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": (where, field), "msg": message, "input": None}])


# =========================================================================== #
# 3. Time helpers
# =========================================================================== #
def now_utc() -> datetime:
    """Current time, truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(moment: datetime | None) -> int | None:
    """UTC datetime -> epoch milliseconds, using exact integer maths (no float rounding)."""
    return None if moment is None else (moment - EPOCH) // timedelta(milliseconds=1)


def hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


# =========================================================================== #
# 4. Business rules (pure functions - easy to test, no database)
# =========================================================================== #
def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings sort like times


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST date of the punch-in; an early-morning punch on an overnight shift belongs to the day before."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `day`."""
    start = datetime.combine(day, hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, hhmm(emp["shift_end"]), IST)
    return start, end + timedelta(days=1) if is_overnight(emp) else end


def late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; minutes counted from shift start."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when that is at least 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value: Decimal, places: str) -> float:
    """R8: half-up rounding (Python's round() is half-to-even and works on inexact floats)."""
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours between the punches, 2 decimals, half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / 3600, "0.01")


def derived_fields(status: str, punch_in: datetime | None, punch_out: datetime | None, emp: dict, day: date) -> dict:
    """The four computed fields of a record (R2-R5). The single place they are worked out."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    punch_in = punch_in.replace(microsecond=0)
    shift_start, shift_end = shift_window(day, emp)
    late = late_minutes(punch_in, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    punch_out = punch_out.replace(microsecond=0)
    hours = work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime_minutes(punch_out, shift_end),
        "half_day": hours < HALF_DAY_BELOW_HOURS,
    }


def check_punch_out(punch_in: datetime, punch_out: datetime, field: str) -> None:
    """A punch-out must come after the punch-in and at most 24 hours later."""
    if not punch_in < punch_out <= punch_in + MAX_SHIFT:
        raise invalid(field, "punch-out must be after punch-in and at most 24 hours later")


def working_days(first: date, last: date) -> int:
    """R7: Monday-Friday days from `first` to `last`, both included (0 if first > last)."""
    return sum((first + timedelta(days=i)).weekday() < 5 for i in range((last - first).days + 1))


# =========================================================================== #
# 5. Converters: MongoDB document -> API JSON (never exposes _id)
# =========================================================================== #
def _api_value(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict) -> dict:
    fields = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {f: doc.get(f) for f in fields} | {"created_at": to_ms(doc.get("created_at"))}


def record_out(doc: dict) -> dict:
    """Missing legacy fields get their documented defaults (0, false, [])."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [
            {
                "at": to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": {
                    field: {"from": _api_value(change["from"]), "to": _api_value(change["to"])}
                    for field, change in entry["changes"].items()
                },
            }
            for entry in doc.get("history") or []
        ],
    }


# =========================================================================== #
# 6. Request models (derived fields are not declared, so they are ignored)
# =========================================================================== #
class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: IsoDate

    @model_validator(mode="after")
    def _shift_has_length(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchIn(BaseModel):
    emp_code: str
    punched_at: EpochMs | None = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOut(BaseModel):
    emp_code: str
    punched_at: EpochMs | None = None


class Regularize(BaseModel):
    status: Status | None = None
    punch_in: EpochMs | None = None
    punch_out: EpochMs | None = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


# =========================================================================== #
# 7. Query builders - shared by each endpoint and by /admin/explain
# =========================================================================== #
# Reusable MongoDB expressions over one attendance record.
WEEKDAY = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Mon..7=Sun
IS_PRESENT = {"$in": ["$status", PRESENCE]}                                                       # R6
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}  # R5
WEEKDAY_PRESENT_VALUE = {"$cond": [{"$lte": [WEEKDAY, 5]}, PRESENT_VALUE, 0]}                      # R7
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up(expr: Any, places: int) -> dict:
    """R8 inside MongoDB. $round is half-to-even, so: floor(x * 10^p + 0.5) / 10^p in Decimal128. null stays null."""
    scale = 10**places
    scaled = {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}
    return {"$toDouble": {"$divide": [{"$floor": scaled}, scale]}}


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def attendance_list_query(
    emp_code: str | None, date_from: str | None, date_to: str | None, status: Status | None, page: int, page_size: int
) -> dict:
    """The filter / sort / page of GET /attendance."""
    if date_from and date_to and date_from > date_to:
        raise invalid("date_from", "date_from must be on or before date_to", "query")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {op: v for op, v in (("$gte", date_from), ("$lte", date_to)) if v}
    if status is not None:
        query["status"] = status.value
    return {"filter": query, "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size, "limit": page_size}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """One employee's totals for a month (runs on attendance_logs)."""
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
            "leave_days": count_if({"$eq": ["$status", Status.LEAVE.value]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


SUMMARY_TOTALS = ("present_days", "hours_sum", "hours_count", "late_count", "total_late_minutes",
                  "leave_count", "on_duty_count")


def department_summary_pipeline(month: str, department: str | None) -> list:
    """Per-department totals for a month (runs on attendance_logs).

    The month's logs are grouped per employee, then $unionWith adds one row for every employee who had
    joined by the month end (R9). Headcount counts those employee rows, so people with zero logs are
    still counted, and logs of unknown, not-yet-joined or other-department employees drop out.
    """
    start, end = month_bounds(month)
    people: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        people["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
            "hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},  # exact decimals
            "hours_count": count_if(has_hours),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": {"$cond": [IS_LATE, "$late_minutes", 0]}},
            "leave_count": count_if({"$eq": ["$status", Status.LEAVE.value]}),
            "on_duty_count": count_if({"$eq": ["$status", Status.ON_DUTY.value]}),
        }},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": people},
            {"$project": {"_id": "$emp_code", "department": 1}},
        ]}},
        # Merge each employee row with its log totals; rows with no employee have no department.
        {"$group": {"_id": "$_id", "department": {"$max": "$department"},
                    **{f: {"$sum": f"${f}"} for f in SUMMARY_TOTALS}}},
        {"$match": {"department": {"$ne": None}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{f: {"$sum": f"${f}"} for f in SUMMARY_TOTALS}}},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": "$headcount",
            "present_days": {"$toDouble": "$present_days"},
            # Mean over records (total hours / number of records), not a mean of per-person means.
            "avg_work_hours": half_up(
                {"$cond": [{"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None]}, 2),
            "late_count": "$late_count",
            "total_late_minutes": "$total_late_minutes",
            "leave_count": "$leave_count",
            "on_duty_count": "$on_duty_count",
        }},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: str | None) -> list:
    """Employees ranked by late minutes with competition ranking (1, 2, 2, 4) (runs on attendance_logs)."""
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}]}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    return pipeline + [
        # $rank gives tied totals the same rank and skips the next ones.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": "$rank", "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": "$total_late_minutes", "late_count": "$late_count"}},
    ]


def trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise invalid("to", "to must be on or after from", "query")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise invalid("to", f"the range can cover at most {MAX_TREND_DAYS} days", "query")
    return first, last


def department_trend_pipeline(department: str, start: str, end: str) -> list:
    """One row per calendar day for a department (runs on employees).

    Every employee becomes a "joined" event (+1 headcount from max(joined_on, start)) and every log
    becomes a "day" event. Events are summed per day, $densify fills the missing days, and two
    window functions give the running headcount and the 7-row moving average.
    """
    first, last = trend_range(start, end)
    joined = {"$lte": ["$joined_on", end]}
    rate = {"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "present": PRESENT_VALUE, "late": {"$cond": [IS_LATE, 1, 0]}}},
            ],
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"date": {"$cond": [joined, {"$max": ["$joined_on", start]}, start]}, "joins": {"$cond": [joined, 1, 0]}}],
            "$logs",
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.date", "joins": {"$sum": "$events.joins"},
                    "present": {"$sum": "$events.present"}, "late": {"$sum": "$events.late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [
            datetime.combine(first, time(), timezone.utc),
            datetime.combine(last + timedelta(days=1), time(), timezone.utc),  # upper bound is exclusive
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}},  # R9
        }}},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present", 0]},
            "late_count": {"$ifNull": ["$late", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, half_up(rate, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            # $avg skips nulls and returns null when the window has no numbers.
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": "$is_working_day",
            "headcount": "$headcount",
            "present_count": {"$toDouble": "$present_count"},
            "late_count": "$late_count",
            "attendance_rate": "$attendance_rate",
            "moving_avg_7d": half_up("$moving_avg_7d", 4),
        }},
    ]


# =========================================================================== #
# 8. Indexes and the FastAPI app
# =========================================================================== #
INDEXES = {
    employees: [
        ([("emp_code", ASCENDING)], {"unique": True}),                   # identity; duplicate creates -> 409
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),     # list by department; trend
        ([("joined_on", ASCENDING)], {}),                                # department summary headcount (R9)
    ],
    logs: [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),  # one record per day; races; lookups
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),               # list sort; month-wide scans
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),  # list by status
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),           # punch-out: latest punch-in
    ],
}


def create_indexes() -> None:
    """Idempotent: creating an index that already exists is a no-op."""
    for collection, specs in INDEXES.items():
        for keys, options in specs:
            collection.create_index(keys, **options)


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
    swagger_ui_parameters={"syntaxHighlight.theme": "idea"},  # light code blocks with dark text
)


# =========================================================================== #
# 9. Endpoints
# =========================================================================== #
def find_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return emp


@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump() | {"created_at": now_utc()}
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees", tags=["employees"])
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    cursor = employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchIn):
    emp = find_employee(body.emp_code)
    at = now_utc() if body.punched_at is None else from_ms(body.punched_at)
    day = attendance_date(at, emp)
    doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": body.status.value,
           "punch_in": at, "punch_out": None, **derived_fields(body.status.value, at, None, emp, day), "history": []}
    try:
        logs.insert_one(doc)  # the unique (emp_code, date) index makes concurrent punch-ins safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOut):
    emp = find_employee(body.emp_code)
    at = now_utc() if body.punched_at is None else from_ms(body.punched_at)
    record = logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$lte": at}}, sort=[("punch_in", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out(record["punch_in"], at, "punched_at")
    fields = derived_fields(record["status"], record["punch_in"], at, emp, date.fromisoformat(record["date"]))
    # Only an open record is updated, so of two simultaneous punch-outs exactly one wins.
    updated = logs.find_one_and_update({"_id": record["_id"], "punch_out": None},
                                       {"$set": {"punch_out": at, **fields}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: str | None = None,
    date_from: Annotated[IsoDate | None, Query()] = None,
    date_to: Annotated[IsoDate | None, Query()] = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    cursor = logs.find(q["filter"], sort=list(q["sort"].items()), skip=q["skip"], limit=q["limit"])
    total = logs.count_documents(q["filter"]) if q["filter"] else logs.estimated_document_count()
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, day_str: Annotated[IsoDate, Path(alias="date")], body: Regularize):
    emp = find_employee(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": day_str})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")
    day = date.fromisoformat(day_str)
    old = {"status": record["status"], "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out"),
           "work_hours": record.get("work_hours"), "late_minutes": record.get("late_minutes") or 0,
           "overtime_minutes": record.get("overtime_minutes") or 0, "half_day": bool(record.get("half_day"))}

    status = old["status"] if body.status is None else body.status.value
    new_in = None if body.punch_in is None else from_ms(body.punch_in)
    new_out = None if body.punch_out is None else from_ms(body.punch_out)
    if status in PRESENCE:
        punch_in, punch_out = new_in or old["punch_in"], new_out or old["punch_out"]
        if punch_in is None:
            raise invalid("punch_in", "a presence status needs a punch_in")
        if new_in is not None and attendance_date(new_in, emp) != day:
            raise invalid("punch_in", "punch_in must stay on the record's attendance date")
        if punch_out is not None:
            check_punch_out(punch_in, punch_out, "punch_out")
    else:
        if new_in is not None or new_out is not None:
            raise invalid("status", f"{status} clears both punch times; do not send punch_in or punch_out")
        punch_in = punch_out = None

    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
           **derived_fields(status, punch_in, punch_out, emp, day)}
    changes = {f: {"from": old[f], "to": new[f]} for f in new if old[f] != new[f]}
    if not changes.keys() & {"status", "punch_in", "punch_out"}:
        raise invalid("body", "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic lock: only write if nobody changed the record since we read it.
    unchanged = {"_id": record["_id"], "status": old["status"], "punch_in": old["punch_in"],
                 "punch_out": old["punch_out"],
                 "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]}}
    updated = logs.find_one_and_update(unchanged, {"$set": new, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was changed at the same time; reload and try again")
    return record_out(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: Annotated[Month, Query()]):
    emp = find_employee(emp_code)
    totals = next(logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    start, end = map(date.fromisoformat, month_bounds(month))
    days = working_days(max(start, date.fromisoformat(emp["joined_on"])), end)
    present = totals.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": float(present),
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present)) * 100 / days, "0.01") if days else None,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: Annotated[Month, Query()], department: str | None = None):
    return {"month": month, "items": list(logs.aggregate(department_summary_pipeline(month, department)))}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(month: Annotated[Month, Query()], limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": list(logs.aggregate(late_leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    start: Annotated[IsoDate, Query(alias="from")],
    end: Annotated[IsoDate, Query(alias="to")],
):
    pipeline = department_trend_pipeline(department, start, end)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, f"department {department} not found")
    return {"department": department, "items": list(employees.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain(
    endpoint: ExplainTarget,
    emp_code: str | None = None,
    month: Annotated[Month | None, Query()] = None,
    department: str | None = None,
    limit: Limit = 10,
    date_from: Annotated[IsoDate | None, Query()] = None,
    date_to: Annotated[IsoDate | None, Query()] = None,
    status: Status | None = None,
    start: Annotated[IsoDate | None, Query(alias="from")] = None,
    end: Annotated[IsoDate | None, Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Runs `explain` (executionStats) on exactly the query the named endpoint runs."""
    def require(**params: Any) -> None:
        missing = [name.rstrip("_") for name, value in params.items() if value is None]
        if missing:
            raise RequestValidationError(
                [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing])

    if endpoint is ExplainTarget.attendance_list:
        q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        collection, command = logs, {"find": logs.name, **q}
    elif endpoint is ExplainTarget.employee_monthly:
        require(emp_code=emp_code, month=month)
        collection, pipeline = logs, employee_monthly_pipeline(emp_code, month)
    elif endpoint is ExplainTarget.department_summary:
        require(month=month)
        collection, pipeline = logs, department_summary_pipeline(month, department)
    elif endpoint is ExplainTarget.late_leaderboard:
        require(month=month)
        collection, pipeline = logs, late_leaderboard_pipeline(month, limit, department)
    else:
        require(department=department, from_=start, to=end)
        collection, pipeline = employees, department_trend_pipeline(department, start, end)
    if endpoint is not ExplainTarget.attendance_list:
        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}

    result = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint.value, "collection": collection.name,
            "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))}
