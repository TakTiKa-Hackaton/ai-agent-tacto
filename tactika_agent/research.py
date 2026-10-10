"""Find relevant procurement evidence and retain only references the arena can verify."""

from __future__ import annotations

class ProcurementResearch:
    def __init__(self, package):
        self.package = package
        self.tables = set(package.tables()) if package else set()

    def _query(self, table, sql, params=(), limit=30):
        if not self.package or table not in self.tables:
            return []
        try:
            return self.package.query(sql, params, limit=limit)
        except (KeyError, OSError, ValueError):
            return []

    def valid_ref(self, case, ref):
        return bool(
            isinstance(ref, str)
            and (ref in (case.get("facts") or {}) or ref in (case.get("refs") or [])
                 or (self.package and self.package.lookup(ref)))
        )

    def _rows_for_case(self, case):
        parts, suppliers = case.get("parts", []), case.get("suppliers", [])
        if not parts:
            return {}, {}, {}, {}
        units = {
            row["id"]: row.get("price_unit")
            for row in self._query(
                "parts", f"SELECT id, price_unit FROM parts WHERE id IN ({','.join('?' for _ in parts)})", parts
            )
        }
        marks = ",".join("?" for _ in suppliers) or "NULL"
        po_rows, quotes, costs, cards = [], [], [], []
        if suppliers:
            for part in parts:
                unit = units.get(part)
                part_pos = self._query(
                    "po_lines",
                    f"SELECT id, part_id, supplier_id, order_date AS date, price, price_unit, currency "
                    f"FROM po_lines WHERE part_id = ? AND supplier_id IN ({marks}) ORDER BY order_date DESC LIMIT 30",
                    [part, *suppliers], 30,
                )
                po_rows += [
                    row for row in part_pos
                    if (unit is None or row.get("price_unit") == unit) and row.get("currency", "EUR") == "EUR"
                ]
                part_quotes = self._query(
                    "quotes",
                    f"SELECT id, part_id, supplier_id, quote_date AS date, unit_price AS price, price_unit, "
                    f"currency, lead_time_days, incoterm, payment_days, awarded FROM quotes "
                    f"WHERE part_id = ? ORDER BY quote_date DESC LIMIT 40",
                    [part], 40,
                )
                quotes += [
                    row for row in part_quotes
                    if row.get("supplier_id") in suppliers and (unit is None or row.get("price_unit") == unit)
                    and row.get("currency", "EUR") == "EUR"
                ]
                part_costs = self._query(
                    "should_cost",
                    f"SELECT id, part_id, supplier_id, estimate_date AS date, unit_cost AS price, price_unit, "
                    f"confidence_pct, method FROM should_cost WHERE part_id = ? "
                    "ORDER BY estimate_date DESC LIMIT 20",
                    [part], 20,
                )
                costs += [
                    row for row in part_costs
                    if (unit is None or row.get("price_unit") == unit)
                    and (not row.get("supplier_id") or row.get("supplier_id") in suppliers)
                ]
            cards = self._query(
                "scorecards",
                f"SELECT * FROM scorecards WHERE supplier_id IN ({marks}) ORDER BY period_end DESC LIMIT 50",
                suppliers, 50,
            )
        return units, po_rows, quotes, {"costs": costs, "cards": cards}

    @staticmethod
    def _month_index(month):
        try:
            year, value = month[:7].split("-")
            return int(year) * 12 + int(value)
        except (AttributeError, TypeError, ValueError):
            return None

    def _market_moves(self, parts):
        if not self.package or not parts:
            return []
        marks = ",".join("?" for _ in parts)
        drivers = self._query(
            "cost_drivers",
            f"SELECT part_id, series_id, weight FROM cost_drivers WHERE part_id IN ({marks})", parts,
        )
        moves = []
        for driver in drivers:
            series = driver.get("series_id")
            ids = self.package.ids(f"idx:{series}@")
            points = []
            for ref in ids:
                fact = self.package.lookup(ref) or {}
                month = fact.get("month") or ref.rsplit("@", 1)[-1]
                value = fact.get("value")
                ix = self._month_index(month)
                if ix is not None and value is not None:
                    points.append((ix, month, float(value), ref))
            points.sort()
            if len(points) < 2:
                continue
            end = points[-1]
            earlier = [point for point in points[:-1] if 0 < end[0] - point[0] <= 12]
            if not earlier or end[2] == 0:
                continue
            start = earlier[0]
            moves.append({
                "series": series,
                "weight": float(driver.get("weight") or 0),
                "change_pct": (end[2] / start[2] - 1) * 100,
                "start_month": start[1],
                "end_month": end[1],
                "start_ref": start[3],
                "end_ref": end[3],
            })
        # One representative series per code; data can list a driver for more than one part.
        unique = {}
        for move in moves:
            unique[move["series"]] = max(unique.get(move["series"], move), move, key=lambda item: item["weight"])
        return sorted(unique.values(), key=lambda item: item["weight"], reverse=True)

    def collect(self, case):
        units, po_rows, quotes, other = self._rows_for_case(case)
        suppliers = set(case.get("suppliers", []))
        latest_cards = {}
        for row in other["cards"]:
            latest_cards.setdefault(row.get("supplier_id"), row)
        po_rows.sort(key=lambda row: row.get("date", ""), reverse=True)
        quotes.sort(key=lambda row: row.get("date", ""), reverse=True)
        costs = [row for row in other["costs"] if row.get("price") is not None]
        costs.sort(key=lambda row: (row.get("supplier_id") in suppliers, row.get("date", "")), reverse=True)
        moves = self._market_moves(case.get("parts", []))

        refs = []
        for row in po_rows + quotes + costs + list(latest_cards.values()):
            ref = row.get("id")
            if self.valid_ref(case, ref) and ref not in refs:
                refs.append(ref)
        for move in moves:
            for ref in (move["start_ref"], move["end_ref"]):
                if self.valid_ref(case, ref) and ref not in refs:
                    refs.append(ref)
        return {
            "units": units,
            "purchase_history": po_rows,
            "quotes": quotes,
            "should_cost": costs,
            "scorecards": latest_cards,
            "market_moves": moves,
            "refs": refs[:20],
        }

    @staticmethod
    def weighted_market_change(research):
        moves = [move for move in research["market_moves"] if move["weight"] > 0]
        weight = sum(move["weight"] for move in moves)
        if not weight:
            return 0.0
        change = sum(move["weight"] * move["change_pct"] for move in moves)
        if weight > 1:
            change /= weight
        return max(-12.0, min(12.0, change))

    def opening_evidence(self, case, research):
        """A short verifiable claim; keep only IDs the case/package can resolve."""
        # A series change is the clearest argument when both endpoints are available.
        for move in research["market_moves"]:
            if self.valid_ref(case, move["start_ref"]) and self.valid_ref(case, move["end_ref"]):
                sentence = (
                    f"{move['series']} moved {move['change_pct']:+.1f}% from {move['start_month']} "
                    f"to {move['end_month']} ({move['start_ref']}, {move['end_ref']})."
                )
                return sentence, [move["start_ref"], move["end_ref"]]

        by_part = {}
        for row in research["purchase_history"]:
            ref = row.get("id")
            if row.get("part_id") and row.get("price") is not None and self.valid_ref(case, ref):
                by_part.setdefault(row["part_id"], []).append(row)
        for part, rows in by_part.items():
            if len(rows) >= 2:
                first, second = rows[:2]
                sentence = (
                    f"Our matching records for {part} list EUR {float(first['price']):.2f} in {first['id']} "
                    f"and EUR {float(second['price']):.2f} in {second['id']}."
                )
                return sentence, [first["id"], second["id"]]
        return "Thank you for your offer. We have reviewed it against the case requirements.", []
