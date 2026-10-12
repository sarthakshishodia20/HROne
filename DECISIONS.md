# DECISIONS.md

## 1. Indexes

| Index | Collection | Fields | Purpose |
|---|---|---|---|
| `emp_code_unique` | `employees` | `emp_code ASC` (unique) | Enforces uniqueness at the DB level, making concurrent duplicate creates safe. Also used by every point-lookup on an employee. |
| `attendance_unique` | `attendance_logs` | `(emp_code ASC, date ASC)` (unique) | The natural key of a record. Enforces the "one record per employee per day" rule atomically, replacing the race-prone check-then-insert. Also hit by PATCH and analytics filters. |
| `attendance_list_sort` | `attendance_logs` | `(date DESC, emp_code ASC)` | Serves the `GET /attendance` list query, whose sort is `date DESC, emp_code ASC`, without a COLLSCAN or in-memory sort on large datasets. |
| `attendance_date_asc` | `attendance_logs` | `(date ASC, emp_code ASC)` | Serves aggregation pipelines that filter by a date range (analytics endpoints). MongoDB can use a range prefix on `date` and then group/sort efficiently. |

**Index I considered and rejected:** a per-`emp_code` index on `attendance_logs`. The `(emp_code, date)` unique compound index already covers point-lookups by employee and the analytics `employee_monthly` filter, so a separate single-field index would be redundant and waste space.

## 2. Punch-in race condition

Two identical punch-in requests arrive at the same instant. Both pass any application-level check before either one inserts. My code removes the pre-check entirely and lets the unique compound index `(emp_code, date)` be the sole gate. One request's `insert_one` lands first and commits; the other hits a `DuplicateKeyError` which pymongo raises as an exception. The endpoint catches `DuplicateKeyError` and returns 409. The client of the losing request gets a clear error with no duplicate data written.

## 3. Ties on the leaderboard

When two employees share the same `total_late_minutes` at the cutoff of `limit`, both are included in the response. The spec uses **standard competition ranking** (1, 2, 2, 4) and says: "return every employee whose `rank <= limit`". My code processes the sorted results in groups of tied employees, assigns the group's rank, and only cuts off after the loop when the next rank would exceed `limit`. So if `limit=2` and employees A (100 min), B (80 min), C (80 min) exist, A gets rank 1, B and C both get rank 2 — all three are returned. The response contains more than `limit` rows, which is correct per the spec.

## 4. Headcount with zero logs

The department summary uses a **lookup-then-aggregate** approach rather than aggregating logs first. It fetches all employees whose `joined_on <= last_day_of_month` from the `employees` collection (R9), then joins log statistics keyed by `emp_code` using a Python dictionary. Employees with no logs simply get a zero contribution for every stat. This means the headcount reflects the true roster size regardless of whether an employee has any attendance records that month.

## 5. One thing I'd change for 100× the data

I would replace the in-Python headcount loop in `department_summary` and `department_trend` with a proper MongoDB `$lookup` + `$group` aggregation pipeline (or a `$facet` that joins employees and logs in a single round trip). Currently, for a large employee roster the Python loop works but consumes memory proportional to headcount. Pushing everything into MongoDB's aggregation engine keeps the data transfer small and lets the server scale horizontally without coordinator memory pressure. I'd also add a partial index on `{date: 1}` filtered to `{late_minutes: {$gt: 0}}` to speed up the leaderboard query.
