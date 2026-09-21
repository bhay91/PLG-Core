# PLG Development — Start Here

Status: Required

## Every New Session

1. Open `PLG-Core` in VS Code.
2. Read `docs/START-HERE.md`.
3. Read `docs/Current-Sprint.md`.
4. Read the active file in `docs/Features/`.
5. Read the latest section of `docs/Session-Log.md`.
6. Run the startup check below.
7. Send the complete output before any code changes are made.

## Startup Check

```bash
cd ~/Desktop/PLG-Core

echo "===== CURRENT SPRINT ====="
cat docs/Current-Sprint.md

echo
echo "===== LATEST SESSION LOG ====="
tail -80 docs/Session-Log.md

echo
echo "===== GIT STATUS ====="
git status --short

echo
echo "===== BRANCH SYNC ====="
git status -sb

echo
echo "===== RECENT COMMITS ====="
git log --oneline --decorate -8

echo
echo "===== LOCAL AND REMOTE HEAD ====="
echo -n "LOCAL:  "
git rev-parse HEAD
echo -n "REMOTE: "
git rev-parse origin/feature/job-workspace
```

## Developer Rule

When Brandon says "Let's continue PLG", always start by reviewing:

- docs/START-HERE.md
- docs/Current-Sprint.md
- docs/Session-Log.md
- Active feature specification

Then request:

- git status --short
- git status -sb
- git log --oneline --decorate -8

Never rely on memory alone.

## Checkpoint Rule

If Brandon says "Checkpoint":

- Stop development.
- Test current work.
- Update documentation.
- Review git diff.
- Commit.
- Push.
- Verify GitHub.
- Continue.
