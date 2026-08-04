# Changelog

## Build 0011 — Universal Registry

- Added persistent registry types for vehicles, machines, engines, marine equipment, generators, trailers, components/assemblies, and other records.
- Added a type-first registration workflow.
- Added type badges and type-aware identifier labels throughout the Registry.
- Updated Registry create, edit, detail, status, and list screens.
- Added a safe database migration and practical backfill for existing records.


## Commit 0010 — Machine Registry Foundation

- Added reusable machine profiles linked to customers.
- Added machine list, create, detail, edit, deactivate, and reactivate workflows.
- Added machine selection to new jobs and automatic machine creation when needed.
- Added machine history links for jobs, quotes, and invoices.
- Backfilled machine profiles from existing jobs without breaking legacy records.
- Added Machines to primary navigation and customer accounts.


## Clean Baseline

- Removed historical installer folders and duplicate extension copies.
- Removed Python cache files.
- Removed old database backup copies from the working project.
- Preserved the current application code, templates, static files, live
  database, and document templates.
- Added `.gitignore`, `VERSION`, and clean development folders.

## Customer Requests v1.0
- Added working Customer Requests intake, search, status filters, reminders, attachments, editing, and deletion.
