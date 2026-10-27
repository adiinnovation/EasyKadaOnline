# EasyKada — Fresh Flask + SQLite deployment package

This package is intended for GitHub + Render. It includes the existing multi-shop grocery management application, Render configuration, Gunicorn, a persistent-disk database path, a health endpoint, a safer standalone 500 error page, and an automated smoke test for admin login, shop creation, shop login, and the shop dashboard.

## First login
- Username: `admin`
- Password: `admin123`

**Immediately change the admin credentials** in Admin Console. Do not use the default password for real shop data.

## GitHub upload
1. Extract the ZIP.
2. Upload the *contents* of this folder to the root of a new or existing GitHub repository (so `app.py` and `render.yaml` are at repository root).
3. Commit the changes.

## Render deployment
1. Open https://dashboard.render.com/.
2. Choose **New + → Blueprint** and connect the repository containing `render.yaml`.
3. Review the service and persistent disk settings, then deploy.
4. The `render.yaml` sets build command `pip install -r requirements.txt`, start command `gunicorn app:app --workers 2 --timeout 120`, health path `/healthz`, and `EASYKADA_DATABASE_PATH=/var/data/easykada.sqlite3`.
5. A persistent disk may require a paid Render plan. Do not enter real data until persistent storage is enabled and confirmed.

If you already have an existing Render service, avoid creating a second service accidentally. Push these files to the connected GitHub repository and manually deploy the latest commit. Confirm the deployed commit hash matches GitHub.

## Diagnose errors
- Open Render → service → Logs.
- Reproduce the issue, then inspect the *new* traceback.
- The 500 handler now uses a standalone HTML response so it will not call the normal template renderer while handling an error. The underlying exception still must be diagnosed from logs.
- `/healthz` checks that the service can access SQLite; it does not guarantee every feature works.

## Local validation
```bash
python -m pip install -r requirements.txt
python -m py_compile app.py
python smoke_test.py
```

## Data and security notes
- SQLite is suitable for a small initial deployment, but concurrent multi-user workloads and production needs should be assessed before scaling.
- Back up the database regularly and test restoring backups.
- Set a unique secret key. Never commit `.env` or real credentials.
- The management P&L is not a full double-entry accounting ledger and this app does not file GST returns.
