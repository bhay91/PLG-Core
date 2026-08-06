# Automatic Timeline

## Status

In Progress

## Release Target

PLG Alpha 8.4

## Purpose

Automatically record important Job activity without requiring manual entry.

## Public Service

`log_job_event()` is the approved reusable method for writing Job Timeline events.

## Current Milestone

- Create the Timeline service.
- Replace the direct Part Status timeline insert.
- Record Service Charge updates.
- Record Sourcing Fee updates.
- Preserve the existing timeline table and UI.

## Rules

- Timeline events belong to the Job.
- Routes should not duplicate timeline SQL.
- Events are written in the same database transaction as the related action.
- No new database migration is required.

## Definition of Done

- [ ] Timeline package exists.
- [ ] `log_job_event()` exists.
- [ ] Part Status changes use the service.
- [ ] Additional Charges updates use the service.
- [ ] Existing timeline UI displays new events.
- [ ] Compile test passes.
- [ ] Browser test passes.
- [ ] Changes committed and pushed.
