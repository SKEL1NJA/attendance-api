# DECISIONS.md

## 1. Indexes

I create five indexes at startup.

- `employees.emp_code` (unique). Every lookup is by this code, and it makes the employee creation race safe. A duplicate insert fails at the database and comes back as 409.
- `employees.department`. Serves the department filter on `GET /employees` and on the department summary.
- `employees.joined_on`. Both cross-collection aggregations start with `joined_on <= last_day`. Without this index, MongoDB scans the whole employees collection.
- `attendance_logs.(emp_code, date)` (unique). Same idea as emp_code: it makes the punch-in race safe. Also used by the per-employee queries in monthly, leaderboard and trend.
- `attendance_logs.(date, -1, emp_code, 1)`. The attendance list sorts by date descending then code ascending. This index serves both the filter and the sort in one pass.

I considered a status index and dropped the idea. Status is low cardinality and almost always combined with a date range, so the date index already cuts the candidate set down. A status index would not change the plan.

## 2. Punch-in race

Two requests for the same employee and day hit `insert_one` at the same time. The unique `(emp_code, date)` index decides the winner. One insert commits, the other throws `DuplicateKeyError`, which the handler catches and returns as 409. There is no read-then-write step, so there is no window for a second request to slip through.

## 3. Ties

The leaderboard returns everyone whose rank is `<= limit`. If two employees tie at rank 10 and limit is 10, both come back. That gives eleven rows, which is what the contract asks for. `limit` is applied after ranking, not before.

## 4. Headcount

The department summary pipeline starts at `employees`, not at `attendance_logs`. Employees with no logs still produce a document, and the group stage counts them. The only filter is `joined_on <= end of month`, so people who join later are left out. Employees in other departments are filtered out too, so a department with zero headcount just does not appear.

## 5. At 100x

Two changes first. The trend endpoint loads every employee's `joined_on` into a Python list and passes it into the pipeline, which will not scale. I would precompute a daily headcount table and update it on employee changes instead. Second, the department summary does one `$lookup` per employee. I would flip the order: group `attendance_logs` by `emp_code` first, then do a single `$lookup` to `employees`, so the join runs once on smaller data. After that I would shard `attendance_logs` on `emp_code`.