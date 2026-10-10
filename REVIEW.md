# REVIEW.md

## Part A — Defects found in `app/main.py`

### Helpers

| # | Where | What is wrong | Symptom | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace check compares floored minutes to 10, so 09:40:01 returns 0. R2 says late if elapsed is strictly more than 10:00. | `compute_late_minutes(09:40:01, "09:30") == 0`; correct is 10. | Compare elapsed seconds to 600, then floor. |
| 2 | `compute_late_minutes` | Treats UTC `punch_in` as if it were in the shift's timezone. Never converts to IST. | Every late employee reports `late_minutes = 0`. | Convert to IST first. |
| 3 | `compute_late_minutes` | No truncation to whole seconds (R1). | 09:40:00.900 crosses the grace boundary. | `replace(microsecond=0)`. |
| 4 | `compute_work_hours` | Python `round` is banker's rounding. R4 requires half-up. | 8h 7m 30s → 8.12; correct is 8.13. | `Decimal` + `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Missing 30-minute minimum from R3. | 5 minutes overtime returns 5; correct is 0. | Threshold check. |
| 6 | `compute_overtime` | Ignores overnight shifts (shift_end on the wrong date). | EMP0005 (22:00–06:00) returns 1480 min instead of 40. | Add one day to shift-end when `shift_end <= shift_start`. |
| 7 | `compute_overtime` | Subtracts aware datetime from possibly naive `punch_out`. | `TypeError` → 500 on first punch-out. | Standardize on aware UTC. |

### Setup

| # | Where | What is wrong | Symptom | Fix |
|---|---|---|---|---|
| 8 | Module top-level | No indexes created at startup. | Duplicate keys possible; analytics do COLLSCAN. | `lifespan` hook creates them idempotently. |
| 9 | `MongoClient(...)` | Missing `tz_aware=True`. Returns naive datetimes. | `.tzinfo is None` on every read. | Pass `tz_aware=True`. |

### Pydantic models

| # | Where | What is wrong | Symptom | Fix |
|---|---|---|---|---|
| 10 | `PunchInIn.punched_at` | No range check. | `1234` returns 201 with a 1970 date. | `Field(ge=100000000000, le=4102444800000)`. |
| 11 | `PunchInIn.status` | Any string accepted. | `{"status": "ABSENT"}` → 201. | `Literal[...]`. |
| 12 | `EmployeeIn` | Missing patterns/lengths on `emp_code`, `email`, `name`, `department`, shifts, `joined_on`. | `emp_code="hello"` → 201. | `Field(pattern=..., min_length=..., ...)`. |
| 13 | `EmployeeIn` | No `shift_start != shift_end` check. | Same-time shift created. | `@model_validator`. |

### Endpoints

| # | Where | What is wrong | Symptom | Fix |
|---|---|---|---|---|
| 14 | `health` | No MongoDB ping. | DB down, `/health` still 200. | Ping, else 503. |
| 15 | `create_employee` | Check-then-insert race. | Parallel creates both succeed. | Unique index + catch `DuplicateKeyError`. |
| 16 | `create_employee` | `datetime.now()` (naive local). | `created_at` shifted by local offset. | `datetime.now(timezone.utc)`. |
| 17 | `create_employee` | Returns `created_at` as ISO string. | Not epoch ms. | Convert. |
| 18 | `list_employees` | `skip = page * page_size` off by one page. | `page=1` returns 21–40. | `(page - 1) * page_size`. |
| 19 | `list_employees` | Total ignores filter. | Dept filter shows global total. | `count_documents(q)`. |
| 20 | `list_employees` | No sort by `emp_code`. | Order depends on insertion. | `.sort("emp_code", 1)`. |
| 21 | `list_employees` | `created_at` ISO string. | Same as #17. | Same fix. |
| 22 | `punch_in` | No 404 for unknown employee. | 500 TypeError. | Explicit 404. |
| 23 | `punch_in` | `datetime.fromtimestamp` uses local tz. | Stored instant wrong off-UTC. | `tz=timezone.utc`. |
| 24 | `punch_in` | Uses local date; ignores overnight rule. | Wrong `date` for late-night and overnight punches. | IST conversion + R1. |
| 25 | `punch_in` | No truncation to seconds. | Microseconds stored. | `replace(microsecond=0)`. |
| 26 | `punch_in` | Check-then-insert race. | Two records for same day. | Unique index on `(emp_code, date)`. |
| 27 | `punch_in` | Adds `id` field. | Not in contract. | Remove. |
| 28 | `punch_in` | Returns `punch_in` as ISO string. | Not epoch ms. | Convert. |
| 29 | `list_attendance` | Loads whole collection in memory. | COLLSCAN, slow at 100k. | Server-side sort/skip/limit. |
| 30 | `list_attendance` | Sort missing `emp_code` tiebreaker. | Unstable tie order. | `[("date", -1), ("emp_code", 1)]`. |
| 31 | `list_attendance` | Adds `id` field. | Not in contract. | Remove. |
| 32 | `list_attendance` | No `date_from <= date_to` check. | Reversed range returns 200. | Raise 422. |
| 33 | `list_attendance` | Datetimes returned as ISO strings. | Also affects `history[].at` and `changes.punch_in/punch_out.from/to`. | Recursive conversion. |

### Checked and not a defect

- `load_dotenv()` without `override=True` — default is `override=False`, so real environment variables correctly win.
- Sync endpoints with PyMongo — FastAPI runs them in a threadpool; the event loop is not blocked.
- `201` status on `POST /employees` and `POST /attendance/punch-in` — correct.
- `pop("_id", None)` — removing `_id` from responses is correct; the problem is what some handlers add back.
- The unused `IST` constant — the constant itself is fine; the bug is that it is never applied (#2, #24).