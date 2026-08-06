# PLG Constitution

Version: 1.0  
Status: Locked

## Article 0 — Single Source of Truth

The `docs/` directory is the official source of truth for PartsLink Global development.

Development decisions must be made from documented requirements, rules, and specifications rather than memory alone.

## Article 1 — The Job Is the Center

The Job is the central record in PLG.

Customer Requests create Jobs. Jobs produce Quotes, Invoices, Parts Order Sheets, reports, and Business Intelligence.

Documents are outputs of the Job, not the primary business record.

## Article 2 — PLG Remembers

PLG should remember what Brandon should not have to remember.

The system should help prevent:

- forgotten work;
- missed revenue;
- repeated manual work;
- missed follow-ups;
- incomplete workflows;
- avoidable errors.

## Article 3 — Business Value First

Every feature must do at least one of the following:

- save time;
- reduce mistakes;
- increase profit.

Features without a clear operational purpose remain in the backlog.

## Article 4 — Small Verified Milestones

Features are built in small, testable milestones.

Each stable milestone must be tested, committed, and pushed before the next major change begins.

## Article 5 — Document Decisions

Business rules, product decisions, lessons learned, and feature specifications must be documented.

The project must not depend on memory alone.

## Article 6 — Build for the Future

Every feature should make future development easier rather than harder.

Prefer:

- reusable components;
- modular architecture;
- shared business rules;
- clear workflows;
- safe migrations;
- minimal duplication.

## Article 7 — Protect Existing Work

New features must not unnecessarily break stable workflows.

Prefer extending existing systems over replacing them.

Database migrations must preserve existing records and use safe defaults.

## Article 8 — Development Lifecycle

Every feature follows this lifecycle:

1. Idea
2. Backlog
3. Specification
4. Design
5. Approval
6. Development
7. Testing
8. Commit
9. Push
10. Release
11. Release Notes

## Article 9 — Feature Responsibility

Every feature must define:

- Problem
- Purpose
- Business Benefit
- Workflow
- Business Rules
- Definition of Done
- Future Integrations
- Release Target
