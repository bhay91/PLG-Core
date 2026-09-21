# `.ppsresearch` format v1

`.ppsresearch` is an offline ZIP transport for imported research evidence. It is not an authoritative PPS record. Uploading it stages a Smart Intake `DRAFT`; an operator must review and explicitly confirm before governed PPS records can be created.

The archive contains exactly:

```text
manifest.json
research.json
source.pdf
```

`manifest.json` is UTF-8 JSON with `format: "ppsresearch"`, `version: 1`, `research: "research.json"`, and `source_pdf: "source.pdf"`. `research.json` must conform to the canonical `ResearchImportPackage` schema in `plg_core/intake/research_import.py`. It may include optional `target_proposal_id` for exact targeting of an existing unconfirmed Smart Intake proposal. `source.pdf` is the original PDF referenced by that schema.

The importer limits archives to 16 members, 8 MiB per member, and 24 MiB total. Absolute paths, traversal, duplicate members, symlinks, nested archives, unsupported members, malformed JSON, and unsupported versions are rejected. Package contents are never executed.

Build one offline package with:

```bash
python scripts/build_ppsresearch.py --research research.json --source source.pdf --output customer-research.ppsresearch
```

The builder validates the canonical schema and PDF before writing the package. It does not access PPS databases, HTTP services, or business records.

When `target_proposal_id` is present, PPS stages an update or add-item proposal for that exact `DRAFT` only. No fuzzy merge is performed, and confirmed or converted proposals are blocked.
