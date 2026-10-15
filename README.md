# Employee Attendance & Analytics API

> **What this repository is:** my solution to the HROne Software Engineer Trainee assignment.
> **What it does:** a FastAPI + MongoDB backend where employees punch in and out, HR corrects records (with an audit
> trail), and analytics (monthly summary, department summary, late leaderboard, daily trend) run as MongoDB
> aggregation pipelines.

## Run it

Needs Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then set MONGO_URI / MONGO_DB
python sample_seed.py                                  # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

Interactive docs: <http://localhost:8000/docs>. Indexes are created automatically at startup.

## Test it

```bash
pytest -q
```

The tests use a separate database (`<MONGO_DB>_test`), which they wipe and refill with hand-made awkward data:
leaderboard ties at the cutoff, weekend records, a mid-month joiner, a future joiner, an employee with no logs, an
overnight shift, legacy records and parallel requests. Expected numbers are worked out by hand in the comments.
I also ran the suite on MongoDB 7.0 and 8.2, and checked every `/admin/explain` plan on a 100,000-record dataset:
IXSCAN everywhere, no COLLSCAN, every endpoint under 150 ms.

## Files

| File | What it is |
|---|---|
| `app/main.py` | The whole application, in nine labelled sections (config, types, time helpers, business rules, converters, request models, query builders, indexes, endpoints). |
| `tests/test_api.py` | End-to-end tests against a real MongoDB. |
| `REVIEW.md` | Part A: the 23 defects found in the starter and how each was fixed. |
| `DECISIONS.md` | Part C: indexes, race handling, ties, headcount, scaling. |
| `sample_seed.py`, `sample_data/` | Loads the nine sample documents from the kit. |

## How the code is structured

- **One job per function.** Rules R1-R5 are small pure functions with no database access, so they are easy to test.
  `derived_fields()` is the only place the four computed fields are worked out, and punch-in, punch-out and
  corrections all call it.
- **One builder per query.** Each endpoint's main query is built by one function (`*_pipeline` / `attendance_list_query`).
  The endpoint and `/admin/explain` call the same builder, so the explained plan is the real one.
- **Thin endpoints.** Handlers only validate, call a builder or rule, and convert the result. They do not compute
  anything themselves.
- **The database guarantees correctness under concurrency.** Unique indexes stop duplicate creates and punch-ins.
  Conditional updates (`punch_out: null`, plus an optimistic check on the record's current state for corrections)
  stop double punch-outs and lost history entries.

## Notes

- `MONGO_URI` and `MONGO_DB` are required. Real environment variables win over `.env`.
- All instants are epoch milliseconds at the API and BSON dates in MongoDB. PyMongo runs with `tz_aware=True`.
- Rounding is half-up everywhere (Python `Decimal`, and `Decimal128` inside pipelines), because MongoDB's `$round`
  and Python's `round()` are half-to-even.
- The trend endpoint's 92-day limit counts both ends: `2026-07-01` to `2026-09-30` (92 days) is allowed.
- Nothing is missing. Every endpoint in the contract is implemented.
