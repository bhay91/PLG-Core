# Developer Startup Tool

## Status

In Progress

## Priority

Critical

## Release Target

PLG Alpha 8.3

## Problem

PLG development currently depends too heavily on chat history and memory.

Uncommitted work, unsynchronized branches, outdated sprint notes, or generated document files can be overlooked when starting a new session.

## Business Goal

Make every PLG development session begin from a verified and documented project state.

## Approved Command

```bash
python3 -m plg_core.devtools.startup
```

## Required Checks

The startup tool must:

- confirm required documentation files exist;
- display the current release;
- display the current feature;
- display the current task;
- display the latest Session Log entry;
- show the active Git branch;
- show the current local commit;
- show the tracked remote commit;
- report whether local and remote commits match;
- show uncommitted and untracked files;
- distinguish generated customer documents from source changes;
- clearly state whether development is safe to continue.

## Required Documentation

- `docs/START-HERE.md`
- `docs/Current-Sprint.md`
- `docs/Session-Log.md`
- `docs/Constitution.md`
- `docs/Development-Standards.md`

## Safety Rules

- The tool is read-only.
- It must not modify files.
- It must not commit or push automatically.
- It must not hide uncommitted changes.
- A Git or documentation error must produce a visible warning.
- The tool must still provide useful output if GitHub is temporarily unavailable.

## Definition of Done

- [x] Purpose and workflow approved.
- [ ] Devtools package exists.
- [ ] Startup command runs.
- [ ] Required documentation is checked.
- [ ] Current sprint information is displayed.
- [ ] Git working-tree state is displayed.
- [ ] Local and remote commits are compared.
- [ ] Generated document changes are identified.
- [ ] Clean repository shows Ready.
- [ ] Uncommitted source work shows Checkpoint Required.
- [ ] Tool compiles.
- [ ] Tool tested from PLG-Core.
- [ ] Documentation updated.
- [ ] Changes committed and pushed.
