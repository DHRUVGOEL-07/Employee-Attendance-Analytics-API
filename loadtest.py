"""
Speed + plan check on ~100,000 attendance records. NOT part of the submission (don't commit it).
Uses a throwaway database named <MONGO_DB>_load (dropped at the end).

    python loadtest.py
"""
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

_base = os.getenv("MONGO_DB", "attendance_db")
os.environ["MONGO_DB"] = _base if _base.endswith("_load") else _base + "_load"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as m  # noqa: E402

assert m.db.name.endswith("_load")
m.client.drop_database(m.db.name)

rnd = random.Random(1)
DEPTS = ["Eng", "Ops", "Sales", "HR", "Finance", "Legal", "Support", "QA", "Design", "Data"]
N_EMP, N_DAYS = 500, 200
start = date(2026, 3, 1)
emps = [dict(emp_code=f"EMP{i:04d}", name=f"Name {i}", email="a@x.io", department=DEPTS[i % 10], shift_start="09:30",
             shift_end="18:30", joined_on="2026-01-01", created_at=datetime.now(timezone.utc)) for i in range(1, N_EMP + 1)]
m.db.employees.insert_many(emps)
logs = []
for e in emps:
    for k in range(N_DAYS):
        d = (start + timedelta(days=k)).isoformat()
        st = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [10, 3, 2, 1, 2])[0]
        pres = st in ("PRESENT", "WFH", "ON_DUTY")
        hrs = round(rnd.uniform(2, 11), 2)
        logs.append(dict(emp_code=e["emp_code"], date=d, status=st, history=[],
                         punch_in=None, punch_out=None, work_hours=hrs if pres else None,
                         late_minutes=rnd.choice([0, 0, 10, 20, 45]) if pres else 0,
                         overtime_minutes=0, half_day=pres and hrs < 4.5))
for i in range(0, len(logs), 10000):
    m.db.attendance_logs.insert_many(logs[i:i + 10000])
print(f"seeded {len(emps)} employees, {len(logs)} logs")
t = time.time(); m.ensure_indexes(); print(f"index build on 100k docs: {time.time() - t:.2f}s\n")

calls = [
    "/attendance?page=1&page_size=20",
    "/attendance?page=500&page_size=100",
    "/attendance?emp_code=EMP0007&date_from=2026-04-01&date_to=2026-04-30",
    "/attendance?status=LEAVE&date_from=2026-04-01&date_to=2026-04-30&page_size=100",
    "/employees?department=Eng",
    "/analytics/employees/EMP0007/monthly?month=2026-04",
    "/analytics/departments/summary?month=2026-04",
    "/analytics/departments/summary?month=2026-04&department=Eng",
    "/analytics/leaderboard/late?month=2026-04&limit=10",
    "/analytics/departments/Eng/trend?from=2026-03-01&to=2026-05-31",
]
bad = 0
with TestClient(m.app) as c:
    for p in calls:
        c.get(p)  # warm-up
        t = time.time(); r = c.get(p); ms = (time.time() - t) * 1000
        flag = "" if r.status_code == 200 and ms < 500 else "   <-- SLOW or ERROR"
        bad += bool(flag)
        print(f"{ms:7.1f} ms  {r.status_code}  {p}{flag}")
    print()
    for name, qs in [("attendance_list", "?emp_code=EMP0007&date_from=2026-04-01&date_to=2026-04-30"),
                     ("attendance_list", "?page=1&page_size=20"), ("attendance_list", "?status=LEAVE&date_from=2026-04-01"),
                     ("employee_monthly", "?emp_code=EMP0007&month=2026-04"), ("department_summary", "?month=2026-04"),
                     ("late_leaderboard", "?month=2026-04&limit=10"),
                     ("department_trend", "?department=Eng&from=2026-03-01&to=2026-05-31")]:
        txt = c.get(f"/admin/explain/{name}{qs}").text
        ok = "IXSCAN" in txt and "COLLSCAN" not in txt
        bad += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] explain {name}{qs}")
m.client.drop_database(m.db.name)
print("\nALL GOOD" if not bad else f"\n{bad} problem(s)")