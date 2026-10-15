"""
Tests for the Employee Attendance & Analytics API
=================================================

What this file is
-----------------
End-to-end checks for `app/main.py`, run against a real MongoDB database.

What it does, in simple words
-----------------------------
* Creates a throw-away database (`MONGO_DB` + "_test") and fills it with
  hand-made data covering the awkward cases the rules describe: ties at the
  leaderboard cutoff, weekend records, a mid-month joiner, an employee who
  joins later, an employee with no logs, an overnight shift, legacy records
  without `half_day` / `history`, and a log for an employee who does not exist.
* Calls every endpoint through FastAPI's TestClient and compares the answers
  with numbers worked out by hand (the comments show the arithmetic).
* Fires parallel requests to prove the race-condition rules hold.

Run:  pytest -q        (needs a running MongoDB; reads MONGO_URI from the environment or .env)
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = os.getenv("MONGO_DB", "attendance_db") + "_test"

from fastapi.testclient import TestClient  # noqa: E402  (env must be set before the app is imported)

from app.main import app, client as mongo, db, late_minutes, work_hours  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h=0, mi=0, s=0, ms=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=IST).astimezone(timezone.utc)


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": ist(2026, 1, 1)}


def log(code, day, status="PRESENT", hours=9.0, late=0, overtime=0, half=False, legacy=False):
    doc = {"emp_code": code, "date": day, "status": status,
           "punch_in": None if status in ("ABSENT", "LEAVE") else ist(*map(int, day.split("-")), 9, 30),
           "punch_out": None, "work_hours": None if status in ("ABSENT", "LEAVE") else hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half, "history": []}
    if legacy:
        for field in ("half_day", "history", "late_minutes", "overtime_minutes"):
            doc.pop(field)
    return doc


@pytest.fixture(scope="module")
def api():
    mongo.drop_database(db.name)
    db.employees.insert_many([
        emp("EMP1001", "QA", "2026-01-01"),
        emp("EMP1002", "QA", "2026-07-15"),            # mid-month joiner, no logs at all
        emp("EMP1003", "QA", "2026-08-10"),            # joins after July
        emp("EMP1004", "Ops", "2026-01-01", "22:00", "06:00"),  # overnight shift
        emp("EMP2001", "Lb", "2026-01-01"), emp("EMP2002", "Lb", "2026-01-01"),
        emp("EMP2003", "Lb", "2026-01-01"), emp("EMP2004", "Lb", "2026-01-01"),
        emp("EMP2005", "Lb2", "2026-01-01"),
        emp("EMP3001", "Tr", "2026-01-01"), emp("EMP3002", "Tr", "2026-07-15"),
        emp("EMP4001", "Solo", "2026-01-01"),          # never has any logs
    ])
    db.attendance_logs.insert_many([
        # EMP1001, July 2026 (1 Jul is a Wednesday)
        log("EMP1001", "2026-07-01", hours=9.0),
        log("EMP1001", "2026-07-02", "WFH", hours=4.0, half=True),
        log("EMP1001", "2026-07-03", "LEAVE"),
        log("EMP1001", "2026-07-04", hours=8.0, late=15, overtime=40),   # Saturday
        log("EMP1001", "2026-07-06", "ON_DUTY", hours=8.5, late=12),
        log("EMP1001", "2026-07-07", "ABSENT"),
        log("EMP1001", "2026-07-08", hours=9.5, legacy=True),
        # Leaderboard: totals 30, 20, 20, 10 in "Lb"; 50 in "Lb2"; 100 for an unknown employee
        log("EMP2001", "2026-07-01", late=30),
        log("EMP2002", "2026-07-01", late=11), log("EMP2002", "2026-07-02", late=9),
        log("EMP2003", "2026-07-01", late=20),
        log("EMP2004", "2026-07-01", late=10),
        log("EMP2005", "2026-07-01", late=50),
        log("EMP9999", "2026-07-01", late=100),
        # Trend department "Tr"
        log("EMP3001", "2026-07-13"),
        log("EMP3001", "2026-07-15", half=True, hours=4.0),
        log("EMP3001", "2026-07-18", late=20),                          # Saturday
    ])
    with TestClient(app) as test_client:
        yield test_client


def by_code(items):
    return {i["emp_code"]: i for i in items}


# --------------------------------------------------------------------------- business rules
def test_late_rule_examples():
    start = ist(2026, 7, 6, 9, 30)
    assert late_minutes(ist(2026, 7, 6, 9, 40, 0), start) == 0
    assert late_minutes(ist(2026, 7, 6, 9, 40, 1), start) == 10
    assert late_minutes(ist(2026, 7, 6, 10, 15, 59), start) == 45


def test_work_hours_rounds_half_up():
    # 5h 03m 18s = 5.055 h exactly; float round() would give 5.05
    assert work_hours(ist(2026, 7, 6, 9), ist(2026, 7, 6, 14, 3, 18)) == 5.06


# --------------------------------------------------------------------------- employees
def test_create_employee_and_validation(api):
    body = {"emp_code": "EMP5001", "name": "New Person", "email": "new@example.com", "department": "QA",
            "joined_on": "2026-09-01"}
    created = api.post("/employees", json=body)
    assert created.status_code == 201
    assert created.json()["shift_start"] == "09:30" and isinstance(created.json()["created_at"], int)
    assert "_id" not in created.json()
    assert api.post("/employees", json=body).status_code == 409
    for bad in ({"emp_code": "E1"}, {"shift_start": "10:00", "shift_end": "10:00"}, {"joined_on": "2026-02-30"},
                {"email": "not-an-email"}, {"shift_start": "24:00"}):
        assert api.post("/employees", json=body | {"emp_code": "EMP5002"} | bad).status_code == 422, bad


def test_concurrent_duplicate_creates_give_one_201(api):
    body = {"emp_code": "EMP5100", "name": "Race", "email": "race@example.com", "department": "QA",
            "joined_on": "2026-09-01"}
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda _: api.post("/employees", json=body).status_code, range(8)))
    assert codes == [201] + [409] * 7


def test_list_employees_filters_sorts_and_pages(api):
    page = api.get("/employees", params={"department": "Lb", "page": 2, "page_size": 2}).json()
    assert page["total"] == 4 and [e["emp_code"] for e in page["items"]] == ["EMP2003", "EMP2004"]
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


# --------------------------------------------------------------------------- punch-in / punch-out
def test_punch_in_rules_and_errors(api):
    # 09:40:00.900 is truncated to 09:40:00, so it is inside the grace period
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": epoch_ms(ist(2026, 8, 3, 9, 40, 0, 900))})
    assert r.status_code == 201
    rec = r.json()
    assert rec["date"] == "2026-08-03" and rec["late_minutes"] == 0 and rec["history"] == []
    assert rec["punch_in"] == epoch_ms(ist(2026, 8, 3, 9, 40)) and rec["work_hours"] is None
    late = api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": epoch_ms(ist(2026, 8, 4, 9, 40, 1)), "status": "WFH"})
    assert late.json()["late_minutes"] == 10 and late.json()["status"] == "WFH"
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": epoch_ms(ist(2026, 8, 3, 11))}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0000"}).status_code == 404
    for bad in (1783312500, 1783312500000.0, "1783312500000", True, {"x": 1}):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": bad}).status_code == 422, bad
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "status": "ABSENT"}).status_code == 422


def test_concurrent_punch_ins_give_one_201(api):
    body = {"emp_code": "EMP4001", "punched_at": epoch_ms(ist(2026, 8, 5, 9, 0))}
    with ThreadPoolExecutor(10) as pool:
        codes = sorted(pool.map(lambda _: api.post("/attendance/punch-in", json=body).status_code, range(10)))
    assert codes == [201] + [409] * 9


def test_overnight_shift_crosses_midnight(api):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP1004", "punched_at": epoch_ms(ist(2026, 8, 3, 21, 55))})
    assert r.json()["date"] == "2026-08-03" and r.json()["late_minutes"] == 0
    out = api.post("/attendance/punch-out", json={"emp_code": "EMP1004", "punched_at": epoch_ms(ist(2026, 8, 4, 6, 40))})
    assert out.status_code == 200
    assert (out.json()["work_hours"], out.json()["overtime_minutes"], out.json()["half_day"]) == (8.75, 40, False)
    # 00:30 is before the 06:00 shift end, so it belongs to the shift that started on the 4th; 2h30m late
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP1004", "punched_at": epoch_ms(ist(2026, 8, 5, 0, 30))})
    assert r.json()["date"] == "2026-08-04" and r.json()["late_minutes"] == 150


def test_punch_out_rules_and_errors(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP2001", "punched_at": epoch_ms(ist(2026, 8, 3, 9, 0))})
    too_late = epoch_ms(ist(2026, 8, 4, 9, 0, 1))
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP2001", "punched_at": too_late}).status_code == 422
    out = api.post("/attendance/punch-out", json={"emp_code": "EMP2001", "punched_at": epoch_ms(ist(2026, 8, 3, 13, 20))})
    assert (out.json()["work_hours"], out.json()["half_day"], out.json()["overtime_minutes"]) == (4.33, True, 0)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP2001", "punched_at": epoch_ms(ist(2026, 8, 3, 14))}).status_code == 409
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP5100"}).status_code == 404   # no punch-in
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0000"}).status_code == 404   # no employee


def test_concurrent_punch_outs_give_one_200(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP2002", "punched_at": epoch_ms(ist(2026, 8, 3, 9, 0))})
    body = {"emp_code": "EMP2002", "punched_at": epoch_ms(ist(2026, 8, 3, 18, 0))}
    with ThreadPoolExecutor(10) as pool:
        codes = sorted(pool.map(lambda _: api.post("/attendance/punch-out", json=body).status_code, range(10)))
    assert codes == [200] + [409] * 9


# --------------------------------------------------------------------------- list
def test_list_attendance(api):
    page = api.get("/attendance", params={"emp_code": "EMP1001", "date_from": "2026-07-02", "date_to": "2026-07-07",
                                          "page_size": 2, "page": 2}).json()
    assert page["total"] == 5 and [i["date"] for i in page["items"]] == ["2026-07-04", "2026-07-03"]
    same_day = api.get("/attendance", params={"date_from": "2026-07-01", "date_to": "2026-07-01"}).json()["items"]
    assert [i["emp_code"] for i in same_day] == sorted(i["emp_code"] for i in same_day)  # tie-break on emp_code
    legacy = api.get("/attendance", params={"emp_code": "EMP1001", "date_from": "2026-07-08", "date_to": "2026-07-08"}).json()
    assert legacy["items"][0]["half_day"] is False and legacy["items"][0]["history"] == []
    assert api.get("/attendance", params={"status": "LEAVE", "emp_code": "EMP1001"}).json()["total"] == 1
    assert api.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-7-1"}).status_code == 422
    assert api.get("/attendance", params={"status": "SICK"}).status_code == 422


# --------------------------------------------------------------------------- regularize
def test_regularize_recomputes_and_appends_history(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP2003", "punched_at": epoch_ms(ist(2026, 8, 3, 10, 5))})
    api.post("/attendance/punch-out", json={"emp_code": "EMP2003", "punched_at": epoch_ms(ist(2026, 8, 3, 19, 0))})
    url = "/attendance/EMP2003/2026-08-03"
    new_in = epoch_ms(ist(2026, 8, 3, 9, 28))
    r = api.patch(url, json={"punch_in": new_in, "reason": "biometric glitch", "regularized_by": "hr.admin",
                             "late_minutes": 999})  # derived fields from a client are ignored
    assert r.status_code == 200
    rec = r.json()
    assert rec["late_minutes"] == 0 and rec["work_hours"] == 9.53 and rec["overtime_minutes"] == 30
    changes = rec["history"][0]["changes"]
    assert changes["punch_in"] == {"from": epoch_ms(ist(2026, 8, 3, 10, 5)), "to": new_in}
    assert changes["late_minutes"] == {"from": 35, "to": 0} and set(changes) == {"punch_in", "late_minutes", "work_hours"}

    absent = api.patch(url, json={"status": "ABSENT", "reason": "was not here", "regularized_by": "hr"}).json()
    assert (absent["punch_in"], absent["punch_out"], absent["work_hours"], absent["overtime_minutes"]) == (None, None, None, 0)
    assert len(absent["history"]) == 2 and absent["history"][1]["changes"]["punch_in"]["to"] is None

    assert api.patch(url, json={"status": "ABSENT", "reason": "same again", "regularized_by": "hr"}).status_code == 422
    assert api.patch(url, json={"status": "LEAVE", "punch_in": new_in, "reason": "bad combo", "regularized_by": "hr"}).status_code == 422
    assert api.patch(url, json={"status": "PRESENT", "reason": "no punch", "regularized_by": "hr"}).status_code == 422
    other_day = epoch_ms(ist(2026, 8, 4, 9, 30))
    assert api.patch(url, json={"status": "PRESENT", "punch_in": other_day, "reason": "wrong day", "regularized_by": "hr"}).status_code == 422
    assert api.patch(url, json={"status": "PRESENT", "reason": "x", "regularized_by": "hr"}).status_code == 422
    assert api.patch("/attendance/EMP2003/2026-08-20", json={"status": "LEAVE", "reason": "nothing", "regularized_by": "hr"}).status_code == 404
    assert api.patch("/attendance/EMP0000/2026-08-03", json={"status": "LEAVE", "reason": "nothing", "regularized_by": "hr"}).status_code == 404


def test_concurrent_regularizations_never_lose_history(api):
    url = "/attendance/EMP4001/2026-08-05"  # created by the punch-in race test
    statuses = ["LEAVE", "ABSENT"] * 4
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda s: api.patch(url, json={"status": s, "reason": "race test", "regularized_by": "hr"}).status_code, statuses))
    assert set(codes) <= {200, 409, 422}
    history = api.get("/attendance", params={"emp_code": "EMP4001", "date_from": "2026-08-05", "date_to": "2026-08-05"}).json()["items"][0]["history"]
    assert len(history) == codes.count(200) >= 1


# --------------------------------------------------------------------------- analytics
def test_employee_monthly(api):
    m = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json()
    # present: 1 Jul (1) + 2 Jul half (0.5) + 6 Jul (1) + 8 Jul (1) = 3.5; the Saturday record is not counted
    assert m["working_days"] == 23 and m["present_days"] == 3.5 and m["leave_days"] == 1
    assert (m["late_count"], m["total_late_minutes"], m["total_overtime_minutes"]) == (2, 27, 40)
    assert m["attendance_pct"] == 15.22  # 3.5 / 23 * 100 = 15.217...
    joiner = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert joiner["working_days"] == 13 and joiner["attendance_pct"] == 0.0
    not_yet = api.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert not_yet["working_days"] == 0 and not_yet["attendance_pct"] is None
    assert api.get("/analytics/employees/EMP0000/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(api):
    items = {i["department"]: i for i in api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    assert list(items) == sorted(items)
    qa = items["QA"]
    assert qa["headcount"] == 2  # EMP1003 joins in August; EMP1002 has no logs but counts
    assert qa["present_days"] == 3.5
    assert qa["avg_work_hours"] == 7.8  # (9 + 4 + 8 + 8.5 + 9.5) / 5 records, weekend included
    assert (qa["late_count"], qa["total_late_minutes"], qa["leave_count"], qa["on_duty_count"]) == (2, 27, 1, 1)
    assert items["Solo"]["headcount"] == 1 and items["Solo"]["avg_work_hours"] is None
    only = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "QA"}).json()["items"]
    assert [i["department"] for i in only] == ["QA"]
    december = api.get("/analytics/departments/summary", params={"month": "2025-12"}).json()["items"]
    assert december == []  # nobody had joined yet


def test_late_leaderboard_ties_and_limit(api):
    def board(**params):
        return [(i["rank"], i["emp_code"], i["total_late_minutes"])
                for i in api.get("/analytics/leaderboard/late", params={"month": "2026-07"} | params).json()["items"]]

    # EMP9999 (100 min) has no employee record and is ignored
    assert board(limit=3) == [(1, "EMP2005", 50), (2, "EMP2001", 30), (3, "EMP1001", 27)]
    assert board(limit=4) == [(1, "EMP2005", 50), (2, "EMP2001", 30), (3, "EMP1001", 27),
                              (4, "EMP2002", 20), (4, "EMP2003", 20), (4, "EMP3001", 20)]
    assert board(limit=2, department="Lb") == [(1, "EMP2001", 30), (2, "EMP2002", 20), (2, "EMP2003", 20)]
    row = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"][0]
    assert row == {"rank": 1, "emp_code": "EMP2005", "name": "Name EMP2005", "department": "Lb2",
                   "total_late_minutes": 50, "late_count": 1}
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_department_trend(api):
    r = api.get("/analytics/departments/Tr/trend", params={"from": "2026-07-12", "to": "2026-07-19"})
    rows = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"],
             i["attendance_rate"], i["moving_avg_7d"]) for i in r.json()["items"]]
    assert rows == [
        ("2026-07-12", False, 1, 0, 0, None, None),   # Sunday, window has no rate yet
        ("2026-07-13", True, 1, 1, 0, 1.0, 1.0),
        ("2026-07-14", True, 1, 0, 0, 0.0, 0.5),      # a working day with no records -> 0.0
        ("2026-07-15", True, 2, 0.5, 0, 0.25, 0.4167),  # EMP3002 joins; (1 + 0 + 0.25) / 3
        ("2026-07-16", True, 2, 0, 0, 0.0, 0.3125),
        ("2026-07-17", True, 2, 0, 0, 0.0, 0.25),
        ("2026-07-18", False, 2, 1, 1, None, 0.25),    # weekend record counts, rate is null
        ("2026-07-19", False, 2, 0, 0, None, 0.25),
    ]
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert api.get("/analytics/departments/Tr/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert len(api.get("/analytics/departments/Tr/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).json()["items"]) == 92
    assert api.get("/analytics/departments/Tr/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422


# --------------------------------------------------------------------------- explain
@pytest.mark.parametrize("endpoint, params", [
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Lb"}),
    ("department_trend", {"department": "Tr", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(api, endpoint, params):
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200 and set(r.json()) == {"endpoint", "collection", "explain"}
    plan = str(r.json()["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan


def test_explain_requires_the_endpoint_params(api):
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Tr"}).status_code == 422
    assert api.get("/admin/explain/unknown").status_code == 422
