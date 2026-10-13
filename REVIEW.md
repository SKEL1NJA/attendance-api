# REVIEW.md

Every defect I found in the starter `app/main.py`, grouped by area. For each one I list where it is, what was wrong, how I would notice it, and how I fixed it.

## Helpers

| # | Where | What is wrong | How you would notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares floored minutes to 10, so a punch at 09:40:01 returns 0. R2 says late only if the elapsed time is strictly more than 10:00. | `compute_late_minutes(09:40:01, "09:30")` returns 0. Correct is 10. | Compare seconds to 600, then floor. |
| 2 | `compute_late_minutes` | Treats a UTC punch_in as if it were already in the shift's timezone. Never converts to IST. | Every late punch reports `late_minutes = 0`. | Convert to IST first. |
| 3 | `compute_late_minutes` | No truncation to whole seconds. R1 requires it before any calculation. | A punch at 09:40:00.900 crosses the grace boundary. | `replace(microsecond=0)` before the math. |
| 4 | `compute_work_hours` | Uses Python `round`, which is banker's rounding. R4 requires half-up. | 8h 7m 30s rounds to 8.12. Correct is 8.13. | Use `Decimal` with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Missing the 30 minute minimum from R3. | Five minutes of overtime returns 5. Correct is 0. | Return 0 when the total is under 30. |
| 6 | `compute_overtime` | Ignores overnight shifts. Uses the attendance date for shift end, but for an overnight shift the end falls on the next day. | Employee with a 22:00 to 06:00 shift and a 06:40 punch out reports 1480 minutes of overtime. Correct is 40. | Add one day to the shift end when `shift_end <= shift_start`. |
| 7 | `compute_overtime` | Subtracts an aware datetime from a possibly naive one. | `TypeError` on the first punch out, which shows up as a 500. | Standardize on aware UTC for everything. |

## Setup

| # | Where | What is wrong | How you would notice it | Fix |
|---|---|---|---|---|
| 8 | Module top level | No indexes created at startup. The seeded database has only the default `_id` index. | Duplicate employees and duplicate punch-ins are possible. Analytics do COLLSCAN. | Create indexes idempotently inside a startup lifespan hook. |
| 9 | `MongoClient(...)` | Missing `tz_aware=True`, so PyMongo returns naive datetimes. | `punch_in.tzinfo` is `None` on every read. Causes defect #7. | Pass `tz_aware=True`. |

## Pydantic models

| # | Where | What is wrong | How you would notice it | Fix |
|---|---|---|---|---|
| 10 | `PunchInIn.punched_at` | No range check on the timestamp. | Sending `1234` returns 201 with a 1970 date. | `Field(ge=100000000000, le=4102444800000)`. |
| 11 | `PunchInIn.status` | Accepts any string. | `{"status": "ABSENT"}` returns 201. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 12 | `EmployeeIn` | Missing patterns for `emp_code`, `email`, `name`, `department`, shifts, and `joined_on`. | `emp_code="hello"` returns 201. | Add patterns and length limits. |
| 13 | `EmployeeIn` | No check that `shift_start` and `shift_end` differ. | A shift with the same start and end is created. | Add a `model_validator`. |

## Endpoints

| # | Where | What is wrong | How you would notice it | Fix |
|---|---|---|---|---|
| 14 | `health` | Never contacts MongoDB, always returns 200. | Stop the database and `/health` still says ok. The runner marks the app ready and every other test fails. | Ping MongoDB in the handler. Return 503 on failure. |
| 15 | `create_employee` | Check then insert, no unique index. | Two parallel creates for the same code both succeed. | Rely on the unique index and catch `DuplicateKeyError` for 409. |
| 16 | `create_employee` | Uses `datetime.now()`, which is naive local time. | `created_at` is shifted by the server's timezone offset. | `datetime.now(timezone.utc)`. |
| 17 | `create_employee` | Returns `created_at` as an ISO string. | Response shows a string, not a number. | Convert to epoch milliseconds. |
| 18 | `list_employees` | `skip = page * page_size`, off by one page. | `page=1` returns employees 21 to 40. | `(page - 1) * page_size`. |
| 19 | `list_employees` | Total ignores the filter. | Filtering by department shows the global total. | `count_documents(q)`. |
| 20 | `list_employees` | No sort. The contract requires ordering by `emp_code` ascending. | Order depends on insert order. | `.sort("emp_code", 1)`. |
| 21 | `list_employees` | Returns `created_at` as ISO. | Same as #17. | Same fix. |
| 22 | `punch_in` | Does not check that the employee exists. | Unknown code gives a `TypeError`, which is a 500. Should be 404. | Explicit 404 when the employee is missing. |
| 23 | `punch_in` | `datetime.fromtimestamp` uses local timezone. | The stored instant is wrong off UTC machines. | Pass `tz=timezone.utc`. |
| 24 | `punch_in` | Uses the local calendar date. Does not handle overnight shifts. | Wrong date for late night punches and for overnight shifts. | Compute the IST date and apply the overnight rule from R1. |
| 25 | `punch_in` | No truncation to whole seconds. | Microseconds survive in the stored punch_in. | `replace(microsecond=0)`. |
| 26 | `punch_in` | Check then insert. Race condition. | Two simultaneous punches create two records. Should be one 201 and one 409. | Unique index plus `DuplicateKeyError` catch. |
| 27 | `punch_in` | Adds an `id` field to the response. | The contract does not have `id` on attendance records. | Do not add it. |
| 28 | `punch_in` | Returns `punch_in` as ISO. | Response is a string. | Convert to epoch ms. |
| 29 | `list_attendance` | Loads the whole collection into Python, sorts with a lambda, paginates with a slice. | Slow at 100k records, COLLSCAN in the plan. | Server side `sort`, `skip`, `limit`. |
| 30 | `list_attendance` | Sort only by date. The contract wants date descending then emp_code ascending. | Ties on the same date can appear in any order. | Tuple sort with both keys. |
| 31 | `list_attendance` | Adds an `id` field. | Same as #27. | Drop it. |
| 32 | `list_attendance` | No check that `date_from <= date_to`. | Reversed range returns an empty page instead of 422. | Check and raise 422. |
| 33 | `list_attendance` | Datetimes returned as ISO, including inside history. | All of `punch_in`, `punch_out`, `history[].at` and the history change values come back as strings. | Use a converter that walks the whole document. |

## Startup

| # | Where | What is wrong | How you would notice it | Fix |
|---|---|---|---|---|
| 34 | `lifespan` | If MongoDB is unreachable the app refuses to start, so `/health` can never report 503. | Stop MongoDB, restart the app. It fails to bind the port instead of coming up and reporting unhealthy. | Catch `PyMongoError`, log a warning, let the app start. |

## Things I checked and decided were fine

- `load_dotenv()` without `override=True`. The default is `override=False`, which is exactly what the environment contract asks for. Real environment variables win over `.env`.
- Sync endpoints with PyMongo. FastAPI runs them in a threadpool, so the event loop is not blocked. Switching to Motor is not required.
- Returning 201 on `POST /employees` and `POST /attendance/punch-in`. This matches the contract.
- Dropping `_id` from responses. Correct. The problem in some endpoints was the extra `id` field that was added back, not the removal of `_id`.
- The `IST` constant being defined but unused in the starter. Not a bug by itself. It was a hint that the author forgot to apply it, which is defects #2 and #24.