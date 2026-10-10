# Employee Attendance & Analytics API

FastAPI service backed by MongoDB.

## Run

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Create a `.env` with `MONGO_URI` and `MONGO_DB`, then:

    uvicorn app.main:app --port 8000

Health check: `GET /health`.