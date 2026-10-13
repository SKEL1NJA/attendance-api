# Employee Attendance & Analytics API

FastAPI service backed by MongoDB. Stores employees and their daily attendance, supports punch in and punch out, manual corrections, and a set of analytics endpoints.

## Run

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Create a `.env` file in the repository root with two lines:

    MONGO_URI=mongodb://localhost:27017
    MONGO_DB=attendance_db

Either point `MONGO_URI` at a local MongoDB (6.0 or newer) or use an Atlas connection string. Then start the app:

    uvicorn app.main:app --port 8000

Health check: `GET /health`. It returns 200 when MongoDB answers and 503 when it does not.

Interactive docs: http://127.0.0.1:8000/docs

## Endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /attendance`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

All indexes are created at startup. No manual setup is needed beyond having a database reachable.

## Notes

Instants in requests and responses are epoch milliseconds. Calendar dates and shift times are strings in IST, as described in the contract.

The database is assumed to be seeded by the grader. The endpoints do not seed anything themselves.