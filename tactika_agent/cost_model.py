"""Independent implementation of the arena's published buyer cost equation."""

from __future__ import annotations

import math
import operator
from datetime import date

OPS = {"<=": operator.le, "<": operator.lt, ">=": operator.ge, ">": operator.gt,
       "==": operator.eq, "!=": operator.ne}
RELATIONSHIP_GOALS = {"stabilise", "secure_supply"}


def closing_terms(case, offer, supplier_standard=None):
    buyer = case["buyer"]
    freight = buyer["freight_eur_per_unit"]
    terms = {
        "volume_per_year": buyer["need_per_year"],
        "contract_months": round(12 * buyer["horizon_years"]),
        "payment_days": buyer["reference_payment_days"],
        "lead_time_days": buyer["need_lead_time_days"],
        "incoterm": max(freight, key=freight.get),
    }
    for layer in (supplier_standard or {}, case.get("fixed_terms", {}), offer or {}):
        for field, value in layer.items():
            if value is not None or field not in terms:
                terms[field] = value
    return terms


def _horizon_start(case):
    fixed_date = case.get("fixed_terms", {}).get("effective_date")
    options = case.get("term_options", {}).get("effective_date", [])
    value = fixed_date or (min((item for item in options if item), default=None))
    return date.fromisoformat(value) if value else None


def _years(case, terms):
    months = terms["contract_months"]
    requested = months / 12 if months else 1.0
    return min(requested, case["buyer"]["horizon_years"])


def _units_at_old_price(case, terms, years):
    start = _horizon_start(case)
    effective = terms.get("effective_date")
    if not start or not effective:
        return 0.0
    lag = (date.fromisoformat(effective) - start).days / 365
    return terms["volume_per_year"] * min(max(lag, 0.0), years)


def _supplier_value(case, terms, supplier):
    buyer = case["buyer"]
    standard = {**(supplier.get("standard") or {}), **case.get("fixed_terms", {})}
    freight = buyer["freight_eur_per_unit"]
    if standard.get("payment_days") is None:
        standard["payment_days"] = buyer["reference_payment_days"]
    if standard.get("incoterm") is None:
        standard["incoterm"] = min(freight, key=freight.get)
    standard.setdefault("tooling_eur", 0.0)
    closed = closing_terms(case, terms, standard)
    years = _years(case, closed)
    quantity = closed["volume_per_year"] * years
    if quantity <= 0:
        return float(closed["unit_price"])
    at_old_rate = _units_at_old_price(case, closed, years) / quantity
    finance_rate = supplier["interest_rate"]
    payment_factor = 1 - finance_rate * (closed["payment_days"] - standard["payment_days"]) / 360
    advance_cash_factor = 1 + (finance_rate - supplier.get("usual_rate", finance_rate)) * years / 2
    ppm = supplier.get("ppm", standard.get("ppm_commitment") or 0)
    return (
        closed["unit_price"] * (1 - at_old_rate) * payment_factor
        + at_old_rate * (buyer.get("current_unit_price") or 0.0) * payment_factor
        + freight[closed["incoterm"]] - freight[standard["incoterm"]]
        + ((closed.get("tooling_eur") or 0) - (standard.get("tooling_eur") or 0)) / quantity * advance_cash_factor
        - ppm / 1_000_000 * ((closed.get("chargeback_eur_per_complaint") or 0)
                             - (standard.get("chargeback_eur_per_complaint") or 0))
        + supplier.get("extra", 0.0)
    )


def _relationship_penalty(case, terms, supplier):
    buyer = case["buyer"]
    required_margin = buyer.get("sustainable_margin_pct")
    weight = buyer.get("relationship_eur_per_point")
    if (buyer.get("category_goal") not in RELATIONSHIP_GOALS or not weight or not required_margin
            or not supplier or not supplier.get("full_cost")):
        return 0.0
    sale_value = _supplier_value(case, terms, supplier)
    if sale_value <= 0:
        return 0.0
    margin = (sale_value - supplier["full_cost"]) / sale_value * 100
    shortfall = max(0.0, required_margin - margin)
    smooth_loss = shortfall**2 / 10 if shortfall <= 5 else shortfall - 2.5
    return weight * smooth_loss * _years(case, closing_terms(case, terms, supplier.get("standard")))


def must_haves_ok(case, offer, supplier_standard=None):
    terms = closing_terms(case, offer, supplier_standard)
    for rule in case["buyer"].get("must_haves", []):
        value = terms.get(rule["field"])
        if value is None and rule["op"] not in {"==", "!="}:
            return False
        if not OPS[rule["op"]](value, rule["value"]):
            return False
    return True


def snap(case, field, value):
    options = case.get("term_options", {}).get(field)
    if not options or value in options:
        return value
    if isinstance(value, (int, float)) and all(isinstance(item, (int, float)) for item in options):
        return min(options, key=lambda item: (abs(item - value), item))
    return options[0]


def complete_offer(case, offer):
    buyer = case["buyer"]
    freight = buyer["freight_eur_per_unit"]
    defaults = {
        "volume_per_year": buyer["need_per_year"],
        "contract_months": round(12 * buyer["horizon_years"]),
        "payment_days": buyer["reference_payment_days"],
        "lead_time_days": buyer["need_lead_time_days"],
        "incoterm": min(freight, key=freight.get),
    }
    result = {}
    for field in case["negotiable"]:
        value = (offer or {}).get(field, defaults.get(field))
        if value is None:
            options = case.get("term_options", {}).get(field, [])
            value = options[0] if options else None
        value = snap(case, field, value)
        result[field] = round(value, 2) if field == "unit_price" and value is not None else value
    return result


def cost_parts(case, offer, supplier_standard=None, supplier=None):
    buyer = case["buyer"]
    terms = closing_terms(case, offer, supplier_standard)
    if terms.get("unit_price") is None:
        raise ValueError("a priced term sheet is required")
    years = _years(case, terms)
    committed = terms["volume_per_year"] * years
    need = buyer["need_per_year"]
    covered = min(terms["volume_per_year"], need) * years
    old_price_qty = _units_at_old_price(case, terms, years)
    new_price_qty = committed - old_price_qty
    spend = old_price_qty * (buyer.get("current_unit_price") or terms["unit_price"])
    spend += new_price_qty * terms["unit_price"]

    shortfall_qty = max(0.0, need * buyer["horizon_years"] - covered)
    shortfall = shortfall_qty * buyer["fallback_unit_price"]
    freight = buyer["freight_eur_per_unit"][terms["incoterm"]] * committed
    defect_exposure = buyer.get("cost_per_defect", 0) - min(
        terms.get("chargeback_eur_per_complaint") or 0, buyer.get("cost_per_defect", 0)
    )
    quality = committed * (terms.get("ppm_commitment") or 0) / 1_000_000 * defect_exposure

    index_cost = index_risk = 0.0
    clause = terms.get("index_clause")
    if clause:
        change = buyer.get("index_paths", {}).get(clause["series"], 0.0)
        over_band = max(0.0, abs(change) - clause["deadband_pct"] / 100)
        index_cost = new_price_qty * terms["unit_price"] * clause["material_share"] * over_band
        index_cost *= math.copysign(1, change)
        index_risk = (new_price_qty * terms["unit_price"] * clause["material_share"]
                      * buyer.get("index_risk_pct", 0) / 100)

    components = {
        "purchase": spend,
        "uncovered_need": shortfall,
        "fallback_uncertainty": shortfall * buyer.get("fallback_risk_pct", 0) / 100,
        "cash_timing": spend * buyer["interest_rate"]
        * (buyer["reference_payment_days"] - terms["payment_days"]) / 360,
        "freight": freight,
        "late_delivery": buyer.get("late_cost_per_day", 0)
        * max(0, terms["lead_time_days"] - buyer["need_lead_time_days"]),
        "quality": quality,
        "tooling": terms.get("tooling_eur") or 0.0,
        "index_movement": index_cost,
        "index_variance": index_risk,
        "supplier_health": _relationship_penalty(case, terms, supplier),
    }
    return components


def total_cost(case, offer, supplier_standard=None, supplier=None):
    if closing_terms(case, offer, supplier_standard).get("unit_price") is None:
        return math.inf
    return round(sum(cost_parts(case, offer, supplier_standard, supplier).values()), 2)
