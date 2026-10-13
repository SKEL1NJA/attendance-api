import logging
import os
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger("uvicorn.error")

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


def _ensure_index(collection, keys, unique: bool = False) -> None:
    existing = {tuple(idx["key"].items()) for idx in collection.list_indexes()}
    if tuple(keys) in existing:
        return
    collection.create_index(keys, unique=unique)


def ensure_indexes() -> None:
    _ensure_index(db.employees, [("emp_code", 1)], unique=True)
    _ensure_index(db.employees, [("department", 1)])
    _ensure_index(db.attendance_logs, [("emp_code", 1), ("date", 1)], unique=True)
    _ensure_index(db.attendance_logs, [("date", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        logger.warning("index creation skipped: %s", exc)
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _employee_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    created = out.get("created_at")
    if isinstance(created, datetime):
        out["created_at"] = to_epoch_ms(created)
    return out


def _attendance_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        if isinstance(out.get(key), datetime):
            out[key] = to_epoch_ms(out[key])
    for entry in out.get("history") or []:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])
        for field, change in (entry.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                for edge in ("from", "to"):
                    if isinstance(change.get(edge), datetime):
                        change[edge] = to_epoch_ms(change[edge])
    return out


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    pin = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if (eh, em) <= (sh, sm) and (pin.hour, pin.minute) < (eh, em):
        pin = pin - timedelta(days=1)
    return pin.date().isoformat()


def count_working_days(start: date_cls, end: date_cls) -> int:
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, remainder = divmod(days, 7)
    working = full_weeks * 5
    weekday_of_start = start.weekday()
    for i in range(remainder):
        if (weekday_of_start + i) % 7 < 5:
            working += 1
    return working


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    pin = punch_in.astimezone(IST).replace(microsecond=0)
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = pin.replace(hour=h, minute=m, second=0)
    elapsed = int((pin - shift_start_dt).total_seconds())
    return 0 if elapsed <= 600 else elapsed // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_date = datetime.fromisoformat(date_str)
    if (eh, em) <= (sh, sm):
        end_date += timedelta(days=1)
    shift_end_dt = end_date.replace(hour=eh, minute=em, tzinfo=IST)
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _check_joined_on(cls, v: str) -> str:
        try:
            date_cls.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return v

    @model_validator(mode="after")
    def _check_shift(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return _employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    items = [_employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    ts = ts.replace(microsecond=0)

    date_str = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return _attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    ts = ts.replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in = record["punch_in"]
    if ts <= punch_in:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if (ts - punch_in).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=422, detail="punched_at more than 24 hours after punch_in"
        )

    work_hours = compute_work_hours(punch_in, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return _attendance_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date_cls, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    date_str = date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    fields_set = body.model_fields_set

    new_status = body.status if body.status is not None else record["status"]

    if "punch_in" in fields_set:
        new_punch_in = (
            datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(
                microsecond=0
            )
            if body.punch_in is not None
            else None
        )
    else:
        new_punch_in = record.get("punch_in")

    if "punch_out" in fields_set:
        new_punch_out = (
            datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(
                microsecond=0
            )
            if body.punch_out is not None
            else None
        )
    else:
        new_punch_out = record.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        if ("punch_in" in fields_set and body.punch_in is not None) or (
            "punch_out" in fields_set and body.punch_out is not None
        ):
            raise HTTPException(
                status_code=422,
                detail="absent or leave status cannot have punch times",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422, detail="presence status requires punch_in"
            )
        computed_date = attendance_date_for(new_punch_in, shift_start, shift_end)
        if computed_date != record["date"]:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in does not fall on attendance date {record['date']}",
            )
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=422, detail="punch_out must be after punch_in"
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out more than 24 hours after punch_in",
                )

    if new_punch_in is not None and new_status in ("PRESENT", "WFH", "ON_DUTY"):
        new_late_minutes = compute_late_minutes(new_punch_in, shift_start)
    else:
        new_late_minutes = 0

    if new_punch_in is not None and new_punch_out is not None:
        new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
        new_overtime_minutes = compute_overtime(
            new_punch_out, shift_start, shift_end, record["date"]
        )
        new_half_day = new_work_hours < 4.50
    else:
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes: dict = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": old_overtime_minutes,
            "to": new_overtime_minutes,
        }
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_punch_in,
            "punch_out": old_punch_out,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="record was modified concurrently")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return _attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: dict = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [_attendance_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    year, mon = map(int, month.split("-"))
    first_day = date_cls(year, mon, 1)
    last_day = date_cls(year, mon, monthrange(year, mon)[1])

    joined = date_cls.fromisoformat(emp["joined_on"])
    eligible_start = max(joined, first_day)
    working_days = count_working_days(eligible_start, last_day)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
            }
        },
        {
            "$facet": {
                "presence": [
                    {"$match": {"status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}}},
                    {
                        "$addFields": {
                            "dow": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    },
                    {"$match": {"dow": {"$in": [2, 3, 4, 5, 6]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {"$cond": ["$half_day", 0.5, 1]}
                            },
                        }
                    },
                ],
                "leave": [
                    {"$match": {"status": "LEAVE"}},
                    {"$count": "leave_days"},
                ],
                "late": [
                    {"$match": {"late_minutes": {"$gt": 0}}},
                    {
                        "$group": {
                            "_id": None,
                            "late_count": {"$sum": 1},
                            "total_late_minutes": {"$sum": "$late_minutes"},
                        }
                    },
                ],
                "overtime": [
                    {
                        "$group": {
                            "_id": None,
                            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                        }
                    }
                ],
            }
        },
    ]

    result = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    presence = result.get("presence") or [{}]
    leave = result.get("leave") or [{}]
    late = result.get("late") or [{}]
    overtime = result.get("overtime") or [{}]

    present_days = float(presence[0].get("present_days", 0.0))
    leave_days = int(leave[0].get("leave_days", 0))
    late_count = int(late[0].get("late_count", 0))
    total_late_minutes = int(late[0].get("total_late_minutes", 0))
    total_overtime_minutes = int(overtime[0].get("total_overtime_minutes", 0))

    if working_days == 0:
        attendance_pct = None
    else:
        pct = Decimal(present_days) / Decimal(working_days) * Decimal(100)
        attendance_pct = float(pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    first_day = date_cls(year, mon, 1)
    last_day = date_cls(year, mon, monthrange(year, mon)[1])
    first_str = first_day.isoformat()
    last_str = last_day.isoformat()

    match_stage: dict = {"joined_on": {"$lte": last_str}}
    if department:
        match_stage["department"] = department

    presence_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$emp"]},
                            "date": {"$gte": first_str, "$lte": last_str},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", presence_statuses]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$ifNull": ["$$l.half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", presence_statuses]},
                                            {
                                                "$ne": [
                                                    {"$ifNull": ["$$l.work_hours", None]},
                                                    None,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$l.status", presence_statuses]},
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$$l.work_hours", None]},
                                            None,
                                        ]
                                    },
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        avg_wh_raw = doc.get("avg_work_hours")
        if avg_wh_raw is None:
            avg_work_hours = None
        else:
            avg_work_hours = float(
                Decimal(str(avg_wh_raw)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        items.append(
            {
                "department": doc["department"],
                "headcount": int(doc["headcount"]),
                "present_days": float(doc["present_days"]),
                "avg_work_hours": avg_work_hours,
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    first_day = date_cls(year, mon, 1)
    last_day = date_cls(year, mon, monthrange(year, mon)[1])

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}