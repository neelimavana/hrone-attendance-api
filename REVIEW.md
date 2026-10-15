# REVIEW.md

> **What this file is:** my code review of the starter `app/main.py` (Part A).
> **What it does:** lists every defect I found, with where it is, how to see it fail and how I fixed it, plus the
> things I checked and decided were fine. Line numbers refer to the **original starter file**.

## Defects

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` L35 | Shift start is built with `punch_in.replace(hour, minute)`, i.e. in the punch's own clock (naive server time / UTC), not IST, and always on the punch's calendar day. | Shift 09:30, punch `2026-07-06T04:35Z` (10:05 IST): start becomes 09:30 *UTC*, giving 0 instead of 35. Overnight 22:00 shift, punch 00:30 IST: start is "today 22:00", giving 0 instead of 150. | `shift_window()` builds the start in IST on the attendance date from R1. `late_minutes()` gets that instant. |
| 2 | `compute_late_minutes` L36-37 | The grace check compares **floored minutes** (`minutes > 10`), so anything in 10:00-10:59 counts as on time. | Shift 09:30, punch 09:40:30: rule R2 says late (10 min), starter gives 0. | Compare **seconds** `> 600`, then return `seconds // 60`. |
| 3 | `compute_work_hours` L41 | `round()` on a float is half-to-even and suffers binary error; R4 needs half-up. Sub-second parts are not truncated either. | 09:00:00 to 14:03:18 = exactly 5.055 h: starter returns 5.05, correct is 5.06. | `work_hours()` truncates to whole seconds and uses `Decimal(seconds) / 3600` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` L47 | No 30-minute minimum (R3). | Punch-out 18:35 on an 18:30 shift: starter gives 5, correct is 0. | `overtime_minutes()` returns 0 below 30. |
| 5 | `compute_overtime` L46 | Overnight shifts: the end is put on the **same** date as the start. | Shift 22:00-06:00, out 06:40 next day: end = start day 06:00, overtime about 1480 min instead of 40. | `shift_window()` moves the end to the next day when `shift_end <= shift_start`. |
| 6 | Module setup L22 | `MongoClient` without `tz_aware=True` returns **naive** datetimes, which then mix with aware ones (`TypeError`, a 500) or are misread as local time. The URI and DB name also have hard-coded fallbacks. | Subtracting a stored `punch_in` from an IST-aware instant raises `can't subtract offset-naive and offset-aware datetimes`. | `MongoClient(..., tz_aware=True)`. `MONGO_URI` / `MONGO_DB` are required, with a clear startup error if missing. |
| 7 | Module setup (missing) | **No indexes are created.** There is no unique guarantee, so every list/analytics query is a COLLSCAN. | `explain` shows `COLLSCAN`. Duplicates appear under the races in #10 and #15. | `create_indexes()` runs at startup (idempotent): unique `employees.emp_code`, unique `(emp_code, date)` and three more (see DECISIONS.md). |
| 8 | `EmployeeIn` L53-60 | No validation: any `emp_code`, email, `HH:MM`, date or length is accepted, and `shift_start == shift_end` is allowed. | `shift_start: "25:99"` is stored and later crashes punch-in (500). `joined_on: "yesterday"` is accepted. | `EmployeeCreate` has the contract's patterns and lengths, a real-date check and a model validator for equal shift times. |
| 9 | `PunchInIn` L65-66 | `punched_at: Optional[int]` is lax: `"1783312500000"` (string), `1783312500000.0` (float) and `1783312500` (seconds) are accepted. `status` is free text. | Seconds input is stored as 21 Jan 1970. `status: "ABSENT"` or `"foo"` creates a record. | `EpochMs` = strict int within `[1e11, 4102444800000]`. `status: PresenceStatus` enum. |
| 10 | `create_employee` L79-83 | Check-then-insert: two concurrent creates both pass `find_one` and both insert. | 8 parallel POSTs with the same `emp_code` give several 201s. | Just `insert_one`. The unique index rejects the loser; `DuplicateKeyError` becomes 409. |
| 11 | `create_employee` L82-85 | `created_at = datetime.now()` is naive server-local time (stored 5h30m off on an IST server) and is returned as an ISO string, not epoch ms. | Response shows `"created_at": "2026-..."`. Contract wants an integer. | `now_utc()` (aware UTC). `employee_out()` returns epoch ms. |
| 12 | `list_employees` L93 | `skip = page * page_size` skips the first page. | `page=1` returns employees 21-40. | `skip = (page - 1) * page_size`. |
| 13 | `list_employees` L94 | `total` ignores the department filter (`count_documents({})`). | `?department=Sales` reports the whole company's count. | `count_documents(query)` with the same filter. |
| 14 | `list_employees` L89, L95 | No sort, so order is arbitrary. `page` / `page_size` are unbounded (`page=0` is a negative skip and a 500; `page_size=0` means "no limit"). | Results come back in insertion order. `?page=0` gives 500. | `.sort("emp_code")`. `page >= 1`, `1 <= page_size <= 100` (422 otherwise). |
| 15 | `punch_in` L104-118 | Check-then-insert race on `(emp_code, date)`. | 10 parallel identical punch-ins create several records for one day. | Unique `(emp_code, date)` index. `insert_one` + `DuplicateKeyError` gives 409. |
| 16 | `punch_in` L101, L113 | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises `TypeError`. | `{"emp_code": "EMP9999"}` gives 500 instead of 404. | `find_employee()` raises 404 first. |
| 17 | `punch_in` L102-103 | `datetime.fromtimestamp(ms / 1000)` is naive **server-local** time with the milliseconds kept. `date` is the server's calendar date, not the IST date (R1), and overnight shifts are ignored. | On a UTC server, a punch at 02:00 IST is filed under the previous day. A 00:30 IST punch on a 22:00-06:00 shift is filed under "today" (R1 wants yesterday). | `from_ms()` gives aware UTC truncated to whole seconds. `attendance_date()` applies R1 in IST. |
| 18 | `punch_in` L102 | `if body.punched_at` treats `0` as "not given" and uses now. | `punched_at: 0` silently becomes the current time. | `is None` check. `0` is rejected by `EpochMs` anyway. |
| 19 | `punch_in` L119-121, `list_attendance` L148-149 | Responses leak an `id` (the ObjectId) and return datetimes as ISO strings, not epoch ms. | Response has `"id": "66a..."` and `"punch_in": "2026-07-06T09:28:00"`. | `record_out()` builds the exact contract shape. No `_id` or `id`; instants are epoch ms (including inside `history[].changes`). |
| 20 | `list_attendance` L144-147 | Loads **every** matching document into Python memory, then sorts and slices there. | Slow and memory-heavy at 100k records. Unfiltered list takes seconds. | MongoDB `find(...).sort(...).skip().limit()` backed by the `{date:-1, emp_code:1}` index. `total` from `count_documents`. |
| 21 | `list_attendance` L145 | Sort is only by `date`. No secondary `emp_code` order, so pages overlap or skip rows on equal dates. | Rows on the same day come back in random order across pages. | Sort `date` desc, then `emp_code` asc. |
| 22 | `list_attendance` L126-131 | No validation: `date_from > date_to`, malformed dates, unknown `status`, `page=0` (empty slice) and huge `page_size` are all accepted. | `?date_from=2026-07-10&date_to=2026-07-01` returns 200 with nothing. `?status=SICK` returns 200. | `IsoDate` type, `Status` enum, page bounds, and 422 when `date_from > date_to`. |
| 23 | `health` L72-74 | Always returns `ok`, even when MongoDB is down. | Stop MongoDB: `/health` still says 200. | `ping` the server. Any `PyMongoError` gives 503. |

## Looked at and decided were **not** defects

- **`load_dotenv()`** (L20) does not override variables that are already set, so real environment variables win as
  required.
- **`IST = timezone(timedelta(hours=5, minutes=30))`** is correct. India has no daylight-saving time, so a fixed
  offset is fine.
- **Dates stored as `"YYYY-MM-DD"` strings** and filtered with `$gte` / `$lte`: zero-padded ISO dates sort the same way
  as strings and as dates, so range queries are correct and can use an index.
- **Identifying employees by `emp_code`** (never `_id`) matches the contract.
- **The initial values on punch-in** (`punch_out: None`, `work_hours: None`, `overtime_minutes: 0`, `half_day: False`,
  `history: []`) are what the data model asks for.
- **`(punch_out - punch_in).total_seconds() / 3600`** in `compute_work_hours` is the right formula. Only the rounding
  is wrong (#3).
- **The `status_code=201`** on both create endpoints is correct.
