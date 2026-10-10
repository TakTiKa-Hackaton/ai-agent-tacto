"""Find relevant procurement evidence and retain only references the arena can verify."""

from __future__ import annotations

import csv
import json
import sqlite3
from functools import cached_property
from pathlib import Path


class ResearchStore:
    """Read-only access to this project's local facts and SQLite research snapshot."""

    def __init__(self, root, release=None):
        self.root = Path(root).expanduser().resolve()
        facts_file = self.root / "facts.json"
        self.facts = json.loads(facts_file.read_text(encoding="utf-8")) if facts_file.is_file() else {}
        release_path = Path(release).expanduser() if release else self.root / "release-sunday"
        if (release_path / "facts.json").is_file():
            self.facts.update(json.loads((release_path / "facts.json").read_text(encoding="utf-8")))
            self.release = release_path.resolve()
        else:
            self.release = None
        databases = sorted(self.root.rglob("*.sqlite")) + sorted(self.root.rglob("*.db"))
        self.connection = None
        if databases:
            path = databases[0].as_posix()
            self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
        if self.connection is None and not self.facts:
            raise FileNotFoundError(f"No research database or fact index found beneath {self.root}")

    def tables(self):
        if not self.connection:
            return {}
        names = [row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        return {
            name: {
                "columns": [row[1] for row in self.connection.execute(f'PRAGMA table_info("{name}")')],
                "rows": self.connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0],
            }
            for name in names
        }

    def query(self, statement, params=(), limit=200):
        if not self.connection:
            return []
        head = statement.lstrip().split(None, 1)[0].lower()
        if head not in {"select", "with"}:
            raise ValueError("ResearchStore accepts read-only SELECT queries")
        cursor = self.connection.execute(statement, params)
        return [dict(row) for row in cursor.fetchmany(limit)]

    @cached_property
    def _id_rows(self):
        index = {}
        for table, info in self.tables().items():
            columns = info["columns"]
            if not columns:
                continue
            key = "id" if "id" in columns else columns[0]
            for row in self.connection.execute(f'SELECT * FROM "{table}"'):
                value = row[key]
                if isinstance(value, str) and ":" in value:
                    index[value] = {"table": table, **dict(row)}
        if self.release:
            for file_path in sorted((self.release / "data").glob("*.csv")):
                with file_path.open(encoding="utf-8", newline="") as stream:
                    for row in csv.DictReader(stream):
                        identifier = row.get("id")
                        if identifier:
                            index[identifier] = {"table": file_path.stem, **row, "root": str(self.release)}
        return index

    def lookup(self, identifier):
        return self.facts.get(identifier) or self._id_rows.get(identifier)

    def ids(self, prefix):
        identifiers = set(self.facts)
        if self.connection:
            identifiers.update(self._id_rows)
        return sorted(item for item in identifiers if item.startswith(prefix))

    def document(self, identifier):
        row = self.lookup(identifier)
        if row:
            for field in ("body", "text", "content"):
                if row.get(field):
                    return row[field]
            base = Path(row.get("root", self.root))
            relative = row.get("path") or row.get("file")
            if not relative:
                return json.dumps(row, ensure_ascii=False)
        else:
            base, relative = self.root, identifier
        target = (base / relative).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise FileNotFoundError(identifier)
        return target.read_text(encoding="utf-8")


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
                    f"SELECT id, part_id, supplier_id, order_date AS date, price, price_unit, currency, price_note "
                    f"FROM po_lines WHERE part_id = ? AND supplier_id IN ({marks}) ORDER BY order_date DESC LIMIT 30",
                    [part, *suppliers], 30,
                )
                po_rows += [
                    row for row in part_pos
                    if (unit is None or row.get("price_unit") == unit) and row.get("currency", "EUR") == "EUR"
                    and row.get("price_note") != "stale_condition"
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
