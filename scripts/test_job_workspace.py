"""Run focused regressions with synthetic databases; never copy a PPS database.

Usage: .venv/bin/python scripts/test_job_workspace.py [pytest arguments]
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="pps-workspace-tests-") as directory:
        root = Path(directory)
        os.environ.update(PPS_DB_PATH=str(root / "seed.db"),
                          PPS_DOCUMENT_ROOT=str(root / "documents"),
                          PPS_UPLOAD_ROOT=str(root / "uploads"))
        # Some older fixtures request a copy of the repository database. Supply a
        # freshly initialized synthetic schema instead, without opening that file.
        protected = (ROOT / "data", Path.home() / "PPS")

        def guard(event, args):
            if event not in {"open", "sqlite3.connect"} or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
                return
            value = os.fsdecode(args[0])
            if value == ":memory:":
                return
            path = Path(value).absolute()
            if any(path == parent or parent in path.parents for parent in protected):
                raise RuntimeError(f"Test attempted to access a protected data path: {path}")

        sys.addaudithook(guard)
        import legacy_app
        from plg_core.database.migrations import run_migrations
        legacy_app.initialize_database()
        run_migrations()
        original_copy = shutil.copy2

        def synthetic_copy(source, destination, *args, **kwargs):
            if Path(source).absolute() == ROOT / "data" / "plg_core.db":
                source = root / "seed.db"
            return original_copy(source, destination, *args, **kwargs)

        import pytest
        defaults = ["tests/test_job_workspace_phase1.py", "tests/test_job_command_center_2.py",
                    "tests/test_parts_research_batch3e.py", "tests/test_simplified_fulfillment.py",
                    "tests/test_mobile_inbox_intake.py"]
        with patch.object(shutil, "copy2", synthetic_copy):
            return pytest.main(sys.argv[1:] or ["-q", *defaults])


if __name__ == "__main__":
    raise SystemExit(main())
