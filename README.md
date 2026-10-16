# IT Asset Tracking System

## Requirements

- Python 3.10+
- MySQL 8.x

## Setup

### 1. Create the database

Fresh install:

```bash
mysql -u root -p < schema.sql
```

Upgrading a database created from an earlier (v3) schema — run once:

```bash
mysql -u root -p assettrack < migrate_v4.sql
```

### 2. Install dependencies

```bash
python -m venv .venv
```

Then activate it — **Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux:**

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Confirm before going further:

```bash
python -c "import flask, pymysql, qrcode, PIL; print('ready')"
```

### 3. Configure

```bash
export DB_USER=root
export DB_PASSWORD=yourpassword
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

Windows uses `set` instead of `export`. Defaults are `localhost` / `root` /
no password / database `assettrack`.

Set `SECRET_KEY` for any real use. Without it the app makes a random key at
startup, so everyone is signed out whenever it restarts. When the app is
served over HTTPS, also set `SESSION_COOKIE_SECURE=1` so the session cookie is
never sent over plain HTTP.

### 4. Create the first administrator

```bash
python app.py create-admin
```

### 5. (Optional) Load demo data

```bash
python seed_data.py
```

### 6. Run

```bash
python app.py
```

Open <http://127.0.0.1:5000>. Set `FLASK_DEBUG=1` to get the auto-reloader and
debugger while developing; leave it off anywhere other people can reach.

### 7. Daily reminders

```bash
python app.py run-checks
```

