"""Company research cache reads/writes with generation ownership checks."""

import json

from ...platform.database import (
    loads_json,
    normalize_application_identity_part,
    now_iso,
    read_connection,
    transaction,
)
from ..companies.public import ensure_company_in_transaction
from ...platform.locale import OutputLocale
from .contracts import COMPANY_CACHE_CONTRACT_VERSION, company_cache_eligibility_hash


def get_company_profile(db_path: str, user_id: str, company: str) -> dict:
    """Read aliases/notes for anchor disambiguation, returning empty when absent."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT aliases_json, notes FROM companies WHERE user_id = ? AND name_key = ?",
            (user_id, normalize_application_identity_part(company)),
        ).fetchone()
    if row is None:
        return {"aliases": [], "notes": ""}
    aliases_json, notes = row
    return {"aliases": loads_json(aliases_json, []), "notes": notes or ""}


def get_research_cache(
    db_path: str,
    user_id: str,
    company: str,
    *,
    output_locale: OutputLocale = "zh-CN",
) -> dict | None:
    """Read current company research cache, returning None when absent."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT research_json, research_time FROM companies "
            "WHERE user_id = ? AND name_key = ?",
            (user_id, normalize_application_identity_part(company)),
        ).fetchone()
    if row is None:
        return None
    research_json, research_time = row
    loaded = loads_json(research_json, None)
    is_localized_envelope = bool(
        isinstance(loaded, dict)
        and loaded.get("cache_version") == COMPANY_CACHE_CONTRACT_VERSION
        and isinstance(loaded.get("localized"), dict)
    )
    if is_localized_envelope:
        entry = loaded["localized"].get(output_locale)
        if not isinstance(entry, dict) or not isinstance(entry.get("report"), dict):
            return None
        return {
            "research": entry["report"],
            "research_time": entry.get("generated_time"),
            "eligibility_hash": entry.get("eligibility_hash"),
            "cache_version": COMPANY_CACHE_CONTRACT_VERSION,
            "content_locale": output_locale,
            "search_profile": entry.get("search_profile") or {},
        }
    is_legacy_envelope = bool(
        isinstance(loaded, dict)
        and isinstance(loaded.get("eligibility_hash"), str)
        and isinstance(loaded.get("report"), dict)
    )
    if output_locale != "zh-CN":
        return None
    return {
        # Existing cache values are Chinese compatibility data. They remain visible but
        # cannot satisfy the new eligibility contract until regenerated.
        "research": loaded.get("report") if is_legacy_envelope else loaded,
        "research_time": research_time,
        "eligibility_hash": None,
        "cache_version": loaded.get("cache_version", 0) if is_legacy_envelope else 0,
        "content_locale": "zh-CN",
        "search_profile": {},
    }


def save_research_cache(
    db_path: str,
    user_id: str,
    company: str,
    report: dict,
    *,
    application_id: int | None = None,
    generation: str | None = None,
    eligibility_hash: str | None = None,
    generated_time: str | None = None,
    output_locale: OutputLocale = "zh-CN",
    search_profile: dict | None = None,
) -> int | None:
    """Save cache, verifying generation ownership in the same transaction."""
    if (application_id is None) != (generation is None):
        raise ValueError("application_id 与 generation 必须同时提供")
    with transaction(db_path) as conn:
        # SELECT does not start a transaction; take the write lock to close the TOCTOU
        # gap between ownership check and cache write.
        conn.execute("BEGIN IMMEDIATE")
        if application_id is not None and generation is not None:
            active = conn.execute(
                "SELECT 1 FROM applications WHERE user_id = ? AND id = ? "
                "AND company = ? AND prep_status IN ('pending', 'running') "
                "AND prep_generation = ?",
                (user_id, application_id, company, generation),
            ).fetchone()
            if active is None:
                return None
        company_id = ensure_company_in_transaction(conn, user_id, company)
        profile_row = conn.execute(
            "SELECT aliases_json, notes FROM companies WHERE id = ? AND user_id = ?",
            (company_id, user_id),
        ).fetchone()
        aliases_json, notes = profile_row if profile_row is not None else (None, None)
        current_eligibility_hash = company_cache_eligibility_hash(
            company=company,
            aliases=loads_json(aliases_json, []),
            notes=notes or "",
            output_locale=output_locale,
            search_profile=search_profile,
        )
        if eligibility_hash is not None and eligibility_hash != current_eligibility_hash:
            return None
        existing_row = conn.execute(
            "SELECT research_json FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        existing = loads_json(existing_row[0], {}) if existing_row else {}
        localized = (
            dict(existing.get("localized") or {})
            if isinstance(existing, dict)
            and existing.get("cache_version") == COMPANY_CACHE_CONTRACT_VERSION
            and isinstance(existing.get("localized"), dict)
            else {}
        )
        stored_time = generated_time or now_iso()
        localized[output_locale] = {
            "content_locale": output_locale,
            "eligibility_hash": current_eligibility_hash,
            "generated_time": stored_time,
            "search_profile": search_profile or {},
            "report": report,
        }
        envelope = {
            "cache_version": COMPANY_CACHE_CONTRACT_VERSION,
            "localized": localized,
        }
        conn.execute(
            "UPDATE companies SET research_json = ?, research_time = ?, updated_time = ? WHERE id = ?",
            (
                json.dumps(envelope, ensure_ascii=False),
                stored_time,
                stored_time,
                company_id,
            ),
        )
        return company_id
