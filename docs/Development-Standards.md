# PLG Development Standards

Status: Active

## Documentation First

Before changing application behavior:

1. Update or create the relevant feature specification.
2. Confirm the business rules.
3. Approve the intended workflow.
4. Then begin implementation.

## Development Workflow

1. Inspect the current code and database.
2. Make one focused change.
3. Compile or validate the changed code.
4. Perform a live end-to-end test when applicable.
5. Inspect `git diff`.
6. Commit only intended source files.
7. Push to GitHub.
8. Verify local and remote branches match.

## Safety Rules

- Never guess about existing database fields or routes.
- Request command output before making assumptions.
- Do not commit generated customer documents with source-code commits.
- Use safe database migrations with defaults.
- Preserve existing records.
- Avoid large unverified changes.
- Stop and request output whenever verification is needed.

## Architecture Rules

- The Job is the center of the workflow.
- Business logic should move into focused service modules.
- Routes should remain thin where practical.
- Shared UI behavior belongs in the design system.
- Avoid page-specific styling unless the page genuinely requires it.
- New features should reuse existing components before creating new ones.

## Git Rules

- Commit after each stable milestone.
- Use clear commit messages describing business and technical changes.
- Push immediately after a successful commit.
- Do not tag unstable or unverified work as final.
- Keep generated customer documents separate from application code.

## Testing Rules

A feature is not complete until:

- changed Python files compile;
- affected routes return successful responses;
- user-facing workflows are manually tested;
- saved data reloads correctly;
- existing workflows still work;
- the Git diff contains only intended changes.
