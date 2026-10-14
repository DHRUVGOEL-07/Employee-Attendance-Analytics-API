"""
Self-check for the analytics + explain endpoints. NOT part of the submission (don't commit it).

Seeds a THROWAWAY database (name forced to end in "_verify") with awkward data - ties, weekend records, an employee
with no logs, a mid-month joiner, a future joiner, orphan logs, missing fields, half days - then compares every
endpoint against a plain-Python reference written independently of the Mongo pipelines.

Run (venv active, MongoDB running, MONGO_URI set or in .env):
    pip install httpx
    python verify_analytics.py
"""
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

if os.getenv("MOCK") == "1":  # only used to self-test this script without MongoDB
    import mongomock, pymongo

    pymongo.MongoClient = mongomock.MongoClient

_base = os.getenv("MONGO_DB", "attendance_db")
os.environ["MONGO_DB"] = _base if _base.endswith("_verify") else _base + "_verify"  # never touch the real database
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as m  # noqa: E402

assert m.db.name.endswith("_verify")
m.client.drop_database(m.db.name)
m.ensure_indexes()

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
rnd = random.Random(7)


def hu(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ----------------------------- dataset ------------------------------------ #
EMPS = [  # code, dept, joined_on
    ("EMP0001", "Eng", "2026-01-05"), ("EMP0002", "Eng", "2026-07-15"), ("EMP0003", "Eng", "2026-01-01"),  # 0003: no logs
    ("EMP0004", "Eng", "2026-08-01"), ("EMP0005", "Ops", "2026-02-01"), ("EMP0006", "Ops", "2026-03-01"),
    ("EMP0007", "Ops", "2026-03-01"), ("EMP0008", "Sales", "2026-07-01"), ("EMP0009", "Sales", "2026-06-10"),
    ("EMP0010", "HR", "2026-09-01"),  # joins after the test window
]
employees = {c: dict(emp_code=c, name=f"Name {c[-2:]}", email="a@x.io", department=d, shift_start="09:30",
                     shift_end="18:30", joined_on=j, created_at=datetime.now(timezone.utc)) for c, d, j in EMPS}
m.db.employees.insert_many([dict(v) for v in employees.values()])

logs = []
d0, d1 = date(2026, 7, 1), date(2026, 8, 10)
for code in list(employees) + ["EMP9999"]:  # EMP9999 does not exist: orphan logs
    if code in ("EMP0003", "EMP0010"):
        continue
    j = employees[code]["joined_on"] if code in employees else "2026-01-01"
    d = d0
    while d <= d1:
        if d.isoformat() >= j and rnd.random() < 0.75:  # missing days are normal
            status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [10, 3, 2, 1, 2])[0]
            doc = dict(emp_code=code, date=d.isoformat(), status=status, history=[])
            if status in PRESENT:
                late = rnd.choice([0, 0, 0, 10, 20, 30, 45, 60])  # multiples create ties
                hrs = round(rnd.uniform(2, 11), 2)
                doc.update(late_minutes=late, work_hours=hrs if rnd.random() < 0.9 else None,
                           overtime_minutes=rnd.choice([0, 0, 30, 45]), half_day=hrs < 4.5)
                if rnd.random() < 0.15:  # seeded docs may omit fields
                    doc.pop("half_day"), doc.pop("late_minutes")
            else:
                doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
            logs.append(doc)
        d += timedelta(days=1)
m.db.attendance_logs.insert_many([dict(x) for x in logs])
print(f"seeded {len(employees)} employees, {len(logs)} logs into {m.db.name}")


# ----------------------------- reference ---------------------------------- #
def month_range(month):
    y, mo = int(month[:4]), int(month[5:])
    first = date(y, mo, 1)
    last = (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1))
    return first.isoformat(), last.isoformat()


def is_wd(ds):
    return date.fromisoformat(ds).weekday() < 5


def units(l):  # presence value of one record, 0.5 for half days
    return (0.5 if l.get("half_day") else 1.0) if l["status"] in PRESENT else 0.0


def ref_monthly(code, month):
    first, last = month_range(month)
    e = employees[code]
    rs = [l for l in logs if l["emp_code"] == code and first <= l["date"] <= last]
    start = max(first, e["joined_on"])
    wd = 0
    if start <= last:
        d = date.fromisoformat(start)
        while d.isoformat() <= last:
            wd += d.weekday() < 5
            d += timedelta(days=1)
    pres = sum(units(l) for l in rs if is_wd(l["date"]))
    return dict(emp_code=code, month=month, working_days=wd, present_days=pres,
                leave_days=sum(l["status"] == "LEAVE" for l in rs),
                late_count=sum((l.get("late_minutes") or 0) > 0 for l in rs),
                total_late_minutes=sum(l.get("late_minutes") or 0 for l in rs),
                total_overtime_minutes=sum(l.get("overtime_minutes") or 0 for l in rs),
                attendance_pct=None if wd == 0 else hu(Decimal(str(pres)) / wd * 100, 2))


def ref_summary(month, dept=None):
    first, last = month_range(month)
    out = []
    for dp in sorted({e["department"] for e in employees.values()}):
        if dept and dp != dept:
            continue
        members = [c for c, e in employees.items() if e["department"] == dp and e["joined_on"] <= last]
        if not members:
            continue
        rs = [l for l in logs if l["emp_code"] in members and first <= l["date"] <= last]
        hrs = [Decimal(str(l["work_hours"])) for l in rs if l["status"] in PRESENT and l.get("work_hours") is not None]
        out.append(dict(department=dp, headcount=len(members),
                        present_days=sum(units(l) for l in rs if is_wd(l["date"])),
                        avg_work_hours=hu(sum(hrs) / len(hrs), 2) if hrs else None,
                        late_count=sum((l.get("late_minutes") or 0) > 0 for l in rs),
                        total_late_minutes=sum(l.get("late_minutes") or 0 for l in rs),
                        leave_count=sum(l["status"] == "LEAVE" for l in rs),
                        on_duty_count=sum(l["status"] == "ON_DUTY" for l in rs)))
    return dict(month=month, items=out)


def ref_leaderboard(month, limit=10, dept=None):
    first, last = month_range(month)
    tot, cnt = {}, {}
    for l in logs:
        lm = l.get("late_minutes") or 0
        if first <= l["date"] <= last and lm > 0 and l["emp_code"] in employees:
            if dept and employees[l["emp_code"]]["department"] != dept:
                continue
            tot[l["emp_code"]] = tot.get(l["emp_code"], 0) + lm
            cnt[l["emp_code"]] = cnt.get(l["emp_code"], 0) + 1
    rows = sorted(tot, key=lambda c: (-tot[c], c))
    out = []
    for i, c in enumerate(rows):
        rank = 1 + sum(1 for o in tot if tot[o] > tot[c])
        if rank <= limit:
            e = employees[c]
            out.append(dict(rank=rank, emp_code=c, name=e["name"], department=e["department"],
                            total_late_minutes=tot[c], late_count=cnt[c]))
    return dict(month=month, items=out)


def ref_trend(dept, d_from, d_to):
    members = {c: e for c, e in employees.items() if e["department"] == dept}
    days, d = [], date.fromisoformat(d_from)
    while d <= date.fromisoformat(d_to):
        days.append(d)
        d += timedelta(days=1)
    rows = []
    for d in days:
        ds = d.isoformat()
        rs = [l for l in logs if l["emp_code"] in members and l["date"] == ds]
        hc = sum(e["joined_on"] <= ds for e in members.values())
        wd = d.weekday() < 5
        pres = sum(units(l) for l in rs)
        rows.append(dict(date=ds, is_working_day=wd, headcount=hc, present_count=pres,
                         late_count=sum((l.get("late_minutes") or 0) > 0 for l in rs),
                         attendance_rate=hu(Decimal(str(pres)) / hc, 4) if wd and hc > 0 else None))
    for i, r in enumerate(rows):
        w = [Decimal(str(x["attendance_rate"])) for x in rows[max(0, i - 6): i + 1] if x["attendance_rate"] is not None]
        r["moving_avg_7d"] = hu(sum(w) / len(w), 4) if w else None
    return dict(department=dept, items=rows)


# ------------------------------- checks ----------------------------------- #
results = []


def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) < 1e-9
    return a == b


def check(name, path, expected):
    try:
        r = client.get(path)
    except NotImplementedError as e:
        results.append((name, "SKIP", f"mongomock: {str(e)[:60]}"))
        return
    if r.status_code != 200:
        results.append((name, "FAIL", f"HTTP {r.status_code} {r.text[:200]}"))
    elif same(r.json(), expected):
        results.append((name, "PASS", ""))
    else:
        got, exp = r.json(), expected
        diff = ""
        if isinstance(got.get("items"), list) and isinstance(exp.get("items"), list):
            for g, e in zip(got["items"], exp["items"]):
                if not same(g, e):
                    diff = f"first differing row:\n   got      {g}\n   expected {e}"
                    break
            else:
                diff = f"row counts differ: got {len(got['items'])}, expected {len(exp['items'])}"
        results.append((name, "FAIL", diff or f"got {got}\n   expected {exp}"))


with TestClient(m.app) as client:
    for month in ("2026-07", "2026-08"):
        for code in employees:
            check(f"monthly {code} {month}", f"/analytics/employees/{code}/monthly?month={month}", ref_monthly(code, month))
        check(f"summary {month}", f"/analytics/departments/summary?month={month}", ref_summary(month))
        check(f"summary {month} Ops", f"/analytics/departments/summary?month={month}&department=Ops", ref_summary(month, "Ops"))
        for lim in (1, 3, 10):
            check(f"leaderboard {month} limit={lim}", f"/analytics/leaderboard/late?month={month}&limit={lim}", ref_leaderboard(month, lim))
        check(f"leaderboard {month} Eng", f"/analytics/leaderboard/late?month={month}&limit=5&department=Eng", ref_leaderboard(month, 5, "Eng"))
    for dp in ("Eng", "Ops", "Sales", "HR"):
        check(f"trend {dp} 41d", f"/analytics/departments/{dp}/trend?from=2026-07-01&to=2026-08-10", ref_trend(dp, "2026-07-01", "2026-08-10"))
    check("trend Eng 1 day", "/analytics/departments/Eng/trend?from=2026-07-15&to=2026-07-15", ref_trend("Eng", "2026-07-15", "2026-07-15"))
    check("trend Sales weekend start", "/analytics/departments/Sales/trend?from=2026-07-04&to=2026-07-20", ref_trend("Sales", "2026-07-04", "2026-07-20"))

    # explain: every plan must use an index and never scan the whole collection
    ex = [
        ("attendance_list", "?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20"),
        ("attendance_list", "?page=1&page_size=20"),
        ("attendance_list", "?status=WFH&date_from=2026-07-01"),
        ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "?month=2026-07"),
        ("department_summary", "?month=2026-07&department=Eng"),
        ("late_leaderboard", "?month=2026-07&limit=10"),
        ("department_trend", "?department=Eng&from=2026-07-01&to=2026-07-31"),
    ]
    for name, qs in ex:
        try:
            r = client.get(f"/admin/explain/{name}{qs}")
        except NotImplementedError as e:  # mongomock has no explain
            results.append((f"explain {name}{qs}", "SKIP", "mongomock")); continue
        except Exception as e:  # noqa: BLE001
            results.append((f"explain {name}{qs}", "SKIP" if os.getenv("MOCK") == "1" else "FAIL", f"{type(e).__name__}: {str(e)[:80]}")); continue
        txt = r.text
        ok = r.status_code == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt
        results.append((f"explain {name}{qs}", "PASS" if ok else "FAIL", "" if ok else f"HTTP {r.status_code} IXSCAN={'IXSCAN' in txt} COLLSCAN={'COLLSCAN' in txt}"))

for name, status, detail in results:
    print(f"[{status}] {name}" + (f"\n   {detail}" if detail else ""))
n_fail = sum(s == "FAIL" for _, s, _ in results)
print(f"\n{sum(s=='PASS' for _,s,_ in results)} passed, {n_fail} failed, {sum(s=='SKIP' for _,s,_ in results)} skipped")
m.client.drop_database(m.db.name)
sys.exit(1 if n_fail else 0)