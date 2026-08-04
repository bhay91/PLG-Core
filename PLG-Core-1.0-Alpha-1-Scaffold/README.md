# PartsLink Global Core — Starter

## Install

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
./run.sh
```

Open:

http://127.0.0.1:8000

## Included in V1 starter

- PLG dashboard
- Automatic job numbering: PLG-J-YYYY-###
- New job form
- Multiple requested parts per job
- Job register
- Job detail page
- Add more parts without starting over
- Job status updates
- Local SQLite database

The database is saved at `data/plg_core.db`.
