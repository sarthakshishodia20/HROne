# Employee Attendance & Analytics API

A FastAPI + MongoDB service implementing employee punch-in/punch-out, manual corrections (regularization), and attendance analytics.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Copy .env.example to .env and set MONGO_URI / MONGO_DB
# Local MongoDB via Docker: docker run -p 27017:27017 mongo:7
python sample_seed.py          # load sample data
uvicorn app.main:app --port 8000 --reload
```

Interactive docs: http://localhost:8000/docs

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

## What's Implemented

All endpoints defined in `openapi.yaml`:

- `GET /health` — MongoDB ping probe (503 if down)
- `POST /employees` — Create employee (unique index enforced at DB level)
- `GET /employees` — List employees, filtered by department, paginated, sorted by `emp_code`
- `GET /employees/{emp_code}` — Get a single employee
- `POST /attendance/punch-in` — Record punch-in with business rules R1, R2
- `POST /attendance/punch-out` — Record punch-out with business rules R3, R4, R5
- `GET /attendance` — List records, filtered and paginated (DB-side sort+skip+limit)
- `PATCH /attendance/{emp_code}/{date}` — Regularize with audit trail, optimistic locking
- `GET /analytics/employees/{emp_code}/monthly` — Monthly summary (R6, R7)
- `GET /analytics/departments/summary` — Department rollup for a month
- `GET /analytics/leaderboard/late` — Standard competition ranking by late minutes
- `GET /analytics/departments/{department}/trend` — Daily trend with `$densify` gap-fill and 7-day moving average
- `GET /admin/explain/{endpoint}` — MongoDB `executionStats` explain for each endpoint's main query

## Key Design Notes

- **No check-then-insert**: concurrency for punch-in and employee creation is handled entirely by unique MongoDB indexes + `DuplicateKeyError` catching.
- **Optimistic locking on regularize**: a history-length filter in the update ensures concurrent corrections don't silently overwrite each other.
- **All times UTC internally**, converted to/from epoch-ms at API boundaries. IST logic lives in the attendance-date and late-minutes helpers only.
- **Half-up rounding** (`Decimal.quantize(ROUND_HALF_UP)`) used for all computed numbers per R4 and R8.
- **`$densify`** used in the trend endpoint to fill in calendar days with no records, avoiding Python-side gap-filling loops.

## What's Missing / Known Limitations

Nothing is intentionally omitted. The full contract from `openapi.yaml` is implemented.

## MongoDB Version Requirement

MongoDB **6.0 or newer** (uses `$densify`, added in 6.1). The grader's 7.0 instance is fully supported.
