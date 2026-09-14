from __future__ import annotations

from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import sqlite3

from legacy_app import get_connection
from plg_core.audit import write_audit

DISPLAY_MODES = ("USD", "JMD", "USD_JMD")
RATE_SOURCES = ("BUSINESS_WORKING_RATE", "MANUAL_OVERRIDE")
UNSET = object()
QUOTE_SNAPSHOT_FIELDS = ("currency_code", "display_currency_mode", "fx_rate", "fx_rate_source", "fx_locked_at")


def _quote_field(quote, field):
    try:
        return quote[field]
    except (KeyError, IndexError, TypeError):
        return None


def is_legacy_quote_currency_snapshot(quote) -> bool:
    """Return whether a quote has no Phase-1 FX snapshot at all."""
    return all(_quote_field(quote, field) is None for field in QUOTE_SNAPSHOT_FIELDS)


def validate_quote_currency_snapshot(quote) -> dict | None:
    """Validate a quote's complete modern snapshot, or return ``None`` for legacy."""
    if is_legacy_quote_currency_snapshot(quote):
        return None
    if _quote_field(quote, "currency_code") != "USD" or any(_quote_field(quote, field) is None for field in QUOTE_SNAPSHOT_FIELDS[1:]):
        raise ValueError("Modern quote currency snapshot is incomplete.")
    try:
        return build_quote_currency_snapshot(
            display_mode=_quote_field(quote, "display_currency_mode"),
            jmd_rate=_quote_field(quote, "fx_rate"),
            rate_source=_quote_field(quote, "fx_rate_source"),
            locked_at=_quote_field(quote, "fx_locked_at"),
        )
    except ValueError:
        raise ValueError("Modern quote currency snapshot is invalid.") from None


def build_quote_currency_snapshot(*, display_mode, jmd_rate, rate_source,
                                  locked_at=None) -> dict:
    """Validate and normalize one immutable USD/JMD quote snapshot."""
    mode = _mode(display_mode)
    rate = canonical_rate(jmd_rate)
    source = str(rate_source or "").strip().upper()
    if source not in RATE_SOURCES:
        raise ValueError("Invalid currency rate source.")
    return {
        "currency_code": "USD",
        "display_currency_mode": mode,
        "fx_rate": rate,
        "fx_rate_source": source,
        "fx_locked_at": locked_at,
    }


def build_revision_currency_defaults_from_quote(quote, *, connection=None) -> dict:
    """Build editable revision FX defaults from a source quote.

    Legacy NULL snapshots intentionally start from the current business
    settings; modern snapshots must be complete and valid.
    """
    if is_legacy_quote_currency_snapshot(quote):
        settings = get_currency_settings(connection)
        return {
            "display_currency_mode": settings["default_display_mode"],
            "fx_rate": settings["jmd_working_rate"],
            "fx_rate_source": "BUSINESS_WORKING_RATE",
        }
    snapshot = validate_quote_currency_snapshot(quote)
    return {key: snapshot[key] for key in ("display_currency_mode", "fx_rate", "fx_rate_source")}


def build_quote_currency_snapshot_from_revision(revision, *, locked_at=None) -> dict:
    """Build a complete successor snapshot from committed revision FX only."""
    if any(revision[field] is None for field in ("display_currency_mode", "fx_rate", "fx_rate_source")):
        raise ValueError("Work Revision currency snapshot is incomplete.")
    try:
        return build_quote_currency_snapshot(
            display_mode=revision["display_currency_mode"],
            jmd_rate=revision["fx_rate"],
            rate_source=revision["fx_rate_source"],
            locked_at=locked_at,
        )
    except ValueError:
        raise ValueError("Work Revision currency snapshot is invalid.") from None


def canonical_rate(value) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("JMD exchange rate must be a finite positive number.")
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError("JMD exchange rate must be a finite positive number.") from None
    if not rate.is_finite() or rate <= 0:
        raise ValueError("JMD exchange rate must be a finite positive number.")
    return format(rate.normalize(), "f")


def _mode(value: str) -> str:
    mode = str(value or "").strip().upper()
    if mode not in DISPLAY_MODES:
        raise ValueError("Invalid currency display mode.")
    return mode


def convert_usd_to_jmd(amount, rate) -> Decimal:
    try:
        if amount is None or isinstance(amount, bool):
            raise ValueError
        usd = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        if not usd.is_finite():
            raise ValueError
        jmd_rate = Decimal(canonical_rate(rate))
    except (InvalidOperation, ValueError):
        raise ValueError("USD amount and JMD rate must be valid numbers.") from None
    return (usd * jmd_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_currency_amount(amount, prefix="US$") -> str:
    """Format an authoritative or converted display amount without float math."""
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount or 0))
    except (InvalidOperation, ValueError):
        raise ValueError("Currency amount must be numeric.") from None
    if not value.is_finite():
        raise ValueError("Currency amount must be finite.")
    return f"{prefix}{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def build_quote_currency_presentation(quote, *, usd_amount=None) -> dict:
    """Prepare immutable quote currency display data; never reads or writes settings."""
    amount = quote["customer_total"] if usd_amount is None else usd_amount
    if is_legacy_quote_currency_snapshot(quote):
        return {
            "is_legacy": True, "mode": "USD", "usd_total": format_currency_amount(amount),
            "jmd_total": None, "rate": None, "rate_display": None,
            "rate_source_label": None, "locked_at": None,
        }
    snapshot = validate_quote_currency_snapshot(quote)
    mode = snapshot["display_currency_mode"]
    jmd = format_currency_amount(convert_usd_to_jmd(amount, snapshot["fx_rate"]), "J$") if mode in {"JMD", "USD_JMD"} else None
    source_label = {"BUSINESS_WORKING_RATE": "Business working rate", "MANUAL_OVERRIDE": "Manual override"}[snapshot["fx_rate_source"]]
    return {
        "is_legacy": False, "mode": mode, "usd_total": format_currency_amount(amount),
        "jmd_total": jmd, "rate": snapshot["fx_rate"],
        "rate_display": f"US$1 = J${format_currency_amount(snapshot['fx_rate'], '').strip('$')}".replace("J$J$", "J$"),
        "rate_source_label": source_label, "locked_at": snapshot["fx_locked_at"],
    }


def get_currency_settings(connection: sqlite3.Connection | None = None) -> dict:
    owned = connection is None
    connection = connection or get_connection()
    try:
        row = connection.execute("SELECT * FROM currency_settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("Currency settings are not initialized.")
        return {
            "id": int(row["id"]), "base_currency": row["base_currency"],
            "jmd_working_rate": canonical_rate(row["jmd_working_rate"]),
            "default_display_mode": _mode(row["default_display_mode"]),
            "updated_at": row["updated_at"],
        }
    finally:
        if owned:
            connection.close()


def update_currency_settings(*, jmd_working_rate=None, default_display_mode=None,
                             actor: str = "system", connection: sqlite3.Connection | None = None) -> dict:
    owned = connection is None
    connection = connection or get_connection()
    try:
        current = get_currency_settings(connection)
        rate = canonical_rate(jmd_working_rate) if jmd_working_rate is not None else current["jmd_working_rate"]
        mode = _mode(default_display_mode) if default_display_mode is not None else current["default_display_mode"]
        if rate != current["jmd_working_rate"] or mode != current["default_display_mode"]:
            connection.execute("UPDATE currency_settings SET jmd_working_rate=?,default_display_mode=?,updated_at=CURRENT_TIMESTAMP WHERE id=1", (rate, mode))
            write_audit(connection, action="CURRENCY_SETTINGS_UPDATED", entity_type="CURRENCY_SETTINGS", entity_id=1,
                        summary="Updated USD/JMD currency settings",
                        metadata={"previous": current, "new": {"jmd_working_rate": rate, "default_display_mode": mode}}, actor=actor)
            if owned:
                connection.commit()
        return get_currency_settings(connection)
    finally:
        if owned:
            connection.close()


def resolve_basket_currency_config(connection: sqlite3.Connection, basket_id: int) -> dict:
    basket = connection.execute("SELECT customer_display_currency_mode_override,customer_jmd_fx_rate_override FROM baskets WHERE id=?", (basket_id,)).fetchone()
    if basket is None:
        raise ValueError("Basket not found.")
    settings = get_currency_settings(connection)
    mode_override = basket["customer_display_currency_mode_override"]
    rate_override = basket["customer_jmd_fx_rate_override"]
    mode = _mode(mode_override) if mode_override is not None else settings["default_display_mode"]
    rate = canonical_rate(rate_override) if rate_override is not None else settings["jmd_working_rate"]
    return {"display_currency_mode": mode, "jmd_working_rate": rate,
            "fx_rate_source": "MANUAL_OVERRIDE" if rate_override else "BUSINESS_WORKING_RATE",
            "display_mode_overridden": bool(mode_override), "rate_overridden": bool(rate_override)}


def set_basket_currency_overrides(connection: sqlite3.Connection, basket_id: int, *,
                                  display_mode=UNSET, jmd_rate=UNSET) -> dict:
    if display_mode is None or jmd_rate is None:
        raise ValueError("Use the explicit clear helper to remove an override.")
    current = connection.execute(
        "SELECT customer_display_currency_mode_override,customer_jmd_fx_rate_override FROM baskets WHERE id=?",
        (basket_id,),
    ).fetchone()
    if current is None:
        raise ValueError("Basket not found.")
    mode = current["customer_display_currency_mode_override"] if display_mode is UNSET else _mode(display_mode)
    rate = current["customer_jmd_fx_rate_override"] if jmd_rate is UNSET else canonical_rate(jmd_rate)
    connection.execute("UPDATE baskets SET customer_display_currency_mode_override=?,customer_jmd_fx_rate_override=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (mode, rate, basket_id))
    return resolve_basket_currency_config(connection, basket_id)


def clear_basket_currency_overrides(connection: sqlite3.Connection, basket_id: int, *,
                                    display_mode=False, jmd_rate=False) -> dict:
    fields = []
    values = []
    if display_mode:
        fields.append("customer_display_currency_mode_override=?"); values.append(None)
    if jmd_rate:
        fields.append("customer_jmd_fx_rate_override=?"); values.append(None)
    if fields:
        values.append(basket_id)
        connection.execute(f"UPDATE baskets SET {','.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
    return resolve_basket_currency_config(connection, basket_id)
