# Employee Attendance & Analytics API

## 🚀 Key Features & Architecture

### 1. Employee Management
* **Strict Validation**: Validates employee codes (`emp_code`) against the `EMP\d{4,6}` format, enforces strict email patterns, and ensures realistic date (`YYYY-MM-DD`) and shift time (`HH:MM`) constraints.
* **Uniqueness & Concurrency**: Enforces unique database indexes on employee codes to prevent duplicates, cleanly mapping `DuplicateKeyError` to HTTP 409 Conflict responses.
* **Pagination & Filtering**: Supports department-based filtering, correct total counts, alphabetical sorting by `emp_code`, and strict pagination bounds (`page >= 1`, `page_size <= 100`).

### 2. Attendance & Shift Tracking
* **Timezone Precision**: Robust handling of IST (Indian Standard Time) and UTC-aware datetimes for shift windows and timestamp conversions.
* **Automated Calculations**:
  * **Late Minutes**: Evaluates grace periods (>10 minutes / 600 seconds) using precise integer division.
  * **Work Hours**: Computes exact work hours with second-level truncation and exact half-up quantization (`ROUND_HALF_UP` via `Decimal`).
  * **Overtime & Half-Days**: Calculates overtime for shifts exceeding minimum thresholds (30 minutes) and flags half-days based on precise hour thresholds (<4.5 hours).
* **Presence Statuses**: Supports distinct presence statuses (`PRESENT`, `WFH`, `ON_DUTY`) alongside absence and leave tracking.

### 3. Regularization & Audit Trail
* **Audit History**: The `PATCH /attendance/{emp_code}/{date}` endpoint allows administrators to regularize attendance records while pushing detailed change logs, timestamps, and reasons into an immutable audit history.
* **Optimistic Concurrency**: Uses state and history-size checks to prevent race conditions during concurrent modifications.

### 4. Advanced Analytics & Reporting
* **Employee Monthly Summary**: Computes monthly presence days, leave days, late counts, total late minutes, overtime minutes, and attendance percentages based on active working days.
* **Department Summary**: Aggregates departmental metrics including headcount, average work hours, leave counts, and on-duty metrics.
* **Late Leaderboard**: Ranks employees by total late minutes using standard competition ranking (`$rank`).
* **Department Trends**: Provides daily trend breakdowns and moving averages over custom date ranges, utilizing MongoDB densification to handle weekends and empty days cleanly.

### 5. Performance, Indexing, & Diagnostics
* **Automatic Indexing**: Idempotent compound and unique index creation (`ensure_indexes`) at startup to eliminate full collection scans (`COLLSCAN`).
* **Health Monitoring**: The `/health` endpoint performs a live database ping (`admin.command("ping")`) and verifies index readiness, returning HTTP 503 if MongoDB is unavailable.
* **Execution Analytics**: The `/admin/explain/{endpoint}` endpoint runs `explain(executionStats)` on underlying queries and aggregation pipelines for diagnostic verification.

---

## 🛠️ Local Setup & Installation

### Prerequisites
* Python 3.10+
* MongoDB 6.0 or newer (or a MongoDB Atlas cluster)

### Step-by-Step Guide
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

