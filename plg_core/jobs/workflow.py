from __future__ import annotations


def derive_machine_work_status(
    *,
    need_count: int,
    parts_found_count: int,
    quote_candidate_count: int,
    covered_need_count: int,
    active_research: bool,
) -> tuple[str, str]:
    """Return a small operator-facing state derived from authoritative work."""
    if (need_count > 0 and covered_need_count >= need_count) or (
        need_count == 0 and quote_candidate_count > 0
    ):
        return "READY", "Ready for Quote"
    if parts_found_count > 0:
        return "PARTS_FOUND", "Parts Found"
    if active_research:
        return "IN_RESEARCH", "In Research"
    return "RESEARCH_NEEDED", "Research Needed"


def summarize_job_work(
    assets: list[dict],
    quote_candidate_count: int,
    unassigned_parts_found_count: int = 0,
) -> dict:
    """Build the Command Center glance counts without storing duplicate state."""
    return {
        "machine_count": len(assets),
        "need_count": sum(int(asset.get("need_count") or 0) for asset in assets),
        "open_need_count": sum(
            int(asset.get("open_need_count") or 0) for asset in assets
        ),
        "parts_found_count": sum(
            int(asset.get("parts_found_count") or 0) for asset in assets
        ) + int(unassigned_parts_found_count or 0),
        "quote_candidate_count": int(quote_candidate_count or 0),
    }
