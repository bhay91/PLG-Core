from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from plg_core.intake.research_import import ResearchImportPackage, unpack_ppsresearch
from plg_core.intake.research_package import build_ppsresearch_package


PDF = b"%PDF-1.4\nminimal test pdf"


def payload():
    return {
        "package_id": "AUTHOR-001", "source_pdf": {"filename": "input.pdf", "sha256": "0" * 64},
        "target": {"mode": "NEW_JOB"}, "customer": {"name": "Test Customer", "company": "Test Co"},
        "machine": {"reference": "machine", "manufacturer": "Maker", "model": "Model", "asset_type": "vehicle", "identifiers": []},
        "requested_needs": [{"reference": "need-1", "original_wording": "Brake part", "quantity": 1}],
        "research_options": [],
    }


def test_builder_round_trips_through_deployed_parser():
    raw = build_ppsresearch_package(payload(), PDF)
    package_data, name, source = unpack_ppsresearch(raw)
    package = ResearchImportPackage.model_validate(package_data)
    assert package.source_pdf.filename == name == "source.pdf"
    assert package.source_pdf.sha256 == hashlib.sha256(PDF).hexdigest()
    assert source == PDF


def test_builder_rejects_bad_schema_and_pdf():
    with pytest.raises(ValueError):
        build_ppsresearch_package({"package_id": "bad"}, PDF)
    with pytest.raises(ValueError):
        build_ppsresearch_package(payload(), b"not a pdf")


def test_builder_output_and_overwrite_policy(tmp_path: Path):
    research = tmp_path / "research.json"
    source = tmp_path / "source.pdf"
    output = tmp_path / "sample.ppsresearch"
    research.write_text(json.dumps(payload()), encoding="utf-8")
    source.write_bytes(PDF)
    command = [sys.executable, "scripts/build_ppsresearch.py", "--research", str(research), "--source", str(source), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "validation: success" in result.stdout
    assert output.exists()
    blocked = subprocess.run(command, capture_output=True, text=True)
    assert blocked.returncode != 0
