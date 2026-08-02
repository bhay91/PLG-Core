# PartsLink Global Core

This is the clean working PLG Core project.

## Start PLG

```bash
cd ~/Desktop/PLG-Core
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

If you already have the working `.venv` in the old project, create a fresh
one here rather than copying it.

Open:

- PLG Core: http://127.0.0.1:8000
- Health check: http://127.0.0.1:8000/health

## Important files

- `app.py` — application entry point
- `legacy_app.py` — current live PLG routes and database initialization
- `plg_core/` — modular application layer
- `templates/` — webpage templates
- `static/` — CSS and browser assets
- `data/plg_core.db` — current live PLG database
- `data/document_templates/` — uploaded corporate document templates

## Firefox extension

The live Firefox extension is stored separately at:

`~/PLG/Firefox-Extension`

It is intentionally not mixed into this Core project.

## GitHub

The live SQLite database is excluded by `.gitignore` to prevent customer and
business data from being uploaded to GitHub accidentally. Keep separate local
backups of the database.
