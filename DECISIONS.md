# DECISIONS.md

> **What this file is:** short answers to the five design questions (Part C).
> **What it does:** explains, in plain words, why the code is built the way it is.

1. **Indexes.** `employees`: unique `emp_code` (identity, 409 on duplicates), `{department, emp_code}` (list by
   department, trend) and `joined_on` (summary headcount). `attendance_logs`: unique `{emp_code, date}` (one record
   per day, punch-in race, PATCH and monthly lookups), `{date: -1, emp_code: 1}` (the list's sort, month scans),
   `{status, date, emp_code}` (status filter) and `{emp_code, punch_in: -1}` (punch-out finds the latest punch-in).
   I rejected a `{date, late_minutes}` index for the leaderboard: the date index already narrows to one month, and
   every extra index slows down writes.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets
   exactly one insert in. MongoDB rejects the other with a duplicate-key error, which I turn into 409. There is no
   "check first" step, so there is no gap to slip through.

3. **Ties.** `$rank` gives equal totals the same rank (1, 2, 2, 4). I filter `rank <= limit` **after** ranking, so
   everyone tied at the cutoff is returned, even if that means more than `limit` rows.

4. **Headcount.** The month's logs are grouped per employee, then `$unionWith` adds one row for every employee who had
   joined by the month end. Headcount counts those employee rows, so someone with zero logs is still one row.

5. **At 100x.** I would keep a daily per-department rollup collection, updated on punch-out and correction. The
   summary and trend would then read about 30 small rows instead of scanning raw logs.
