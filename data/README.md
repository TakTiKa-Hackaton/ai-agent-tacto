# Hollerau Pumpenwerke - procurement data package v1.0.0

You are the purchasing team of Hollerau Pumpenwerke GmbH, a fictional pump maker (start with
`company.md`). Everything is as of **2026-09-30**. Company data is synthetic; market series are real
open data (`ATTRIBUTION.md`). All company names are fictional; any match with a real company is
coincidental.

## Files
- `data/*.csv` - the master copy (UTF-8, comma, ISO dates, dot decimals); schema in `datapackage.json`.
- `world.sqlite` - the same tables in one SQLite file, generated from the CSVs.
- `facts.json` - every ID you can cite in a claim, with the values the arena checks.
- Prices are per `price_unit` (1, 100 or 1,000 pieces, kg or m), as the ERP states them; cases
  negotiate in the same unit.
- `company.md`, `SCORECARD.md`, `KNOWN_ISSUES.md`, `ATTRIBUTION.md`; contract texts in `contracts/`,
  letters, e-mails and notes in `correspondence/`.

## IDs
`<type>:<id>[@period]`, unique across the package, in the first column of each table: `sup:70110`,
`part:310010`, `po:4500010001-10`, `gr:`, `inv:`, `qn:`, `ctr:4600070110` (clause `ctr:4600070110#3.1`),
`quote:`, `rfq:`, `fc:310010@2026-09`, `idx:PPI-C2451@2026-08`, `sc:70110@2026Q3`, `cost:310010`,
`news:`, `doc:`.

## Tables
| Table | Rows | Content |
|---|---|---|
| `suppliers` | 447 | Vendor master: the detailed suppliers (`detail = full`) and the long tail (`spend only`). |
| `supplier_intel` | 46 | Public face of each detailed supplier: register facts, filed turnover, credit index, audit. |
| `parts` | 60 | Material master of the detailed parts. |
| `sourcing` | 90 | Source list: approved suppliers per part, quota, tooling owner. |
| `price_conditions` | 339 | Price history per part and supplier (price per `price_unit`), with the event behind it. |
| `negotiation_history` | 300 | Past price events: renewals with asked and settled change. |
| `contracts` | 46 | Framework agreements (text in `contracts/`). |
| `contract_clauses` | 280 | Clauses of the framework agreements, incl. index clauses (`ctr:<no>#<clause>`). |
| `po_lines` | 5400 | Purchase order lines: requested and first confirmed date, price per `price_unit`. |
| `goods_receipts` | 5580 | Goods receipts per PO line (posting date, quantity). |
| `invoices` | 5578 | Supplier invoices with due and payment dates, cash discount taken. |
| `quality_notifications` | 1033 | Complaints against suppliers (QN), with 8D dates and recharged fees. |
| `scorecards` | 414 | Rolling 12-month supplier scorecards per quarter (method: `SCORECARD.md`). |
| `forecasts` | 720 | Quarterly demand forecast snapshots per part, shared with suppliers. |
| `forecast_lines` | 8640 | Monthly quantities of each forecast snapshot. |
| `rfqs` | 47 | Requests for quotation: past benchmarks, second-source quotes, open RFQs for new parts. |
| `quotes` | 108 | Supplier quotes (price per `price_unit`); missing fields as the supplier sent them. |
| `spend_cube` | 1137 | Spend per fiscal year and supplier, incl. the long tail. |
| `index_series` | 33 | Market series: source, licence, unit; derived series with their formula. |
| `index_values` | 2258 | Monthly values of the market series (`idx:<series>@<YYYY-MM>`). |
| `cost_drivers` | 166 | The category team's estimate of each part's cost drivers (index series and weight). |
| `should_cost` | 28 | Bottom-up should-cost estimates for selected parts (`cost:<part>`). |
| `news` | 39 | Press items about suppliers and markets. |
| `documents` | 208 | Letters, e-mails, meeting and call notes (text in `correspondence/`). |

## How the tables connect
```
suppliers 1-n sourcing n-1 parts          parts 1-n price_conditions n-1 suppliers
suppliers 1-1 supplier_intel, 1-n contracts 1-n contract_clauses
po_lines -> goods_receipts -> invoices;   goods_receipts 1-n quality_notifications
parts 1-n forecasts 1-n forecast_lines;   rfqs 1-n quotes;   index_series 1-n index_values
suppliers -> scorecards, spend_cube, news, documents;   parts -> cost_drivers, should_cost
```

## Example queries (SQLite)
```sql
-- spend by supplier, last fiscal year
SELECT s.name, c.spend_eur FROM spend_cube c JOIN suppliers s ON s.id = c.supplier_id
WHERE c.fiscal_year = 'FY2026' ORDER BY c.spend_eur DESC LIMIT 10;
-- price history of one part
SELECT supplier_id, valid_from, price, price_unit FROM price_conditions WHERE part_id = 'part:310010';
-- latest scorecard per supplier
SELECT supplier_id, otd_pct, ppm, colour FROM scorecards WHERE quarter = '2026Q3' ORDER BY score;
-- an index over the last year
SELECT month, value FROM index_values WHERE series_id = 'PPI-C2451' AND month >= '2025-08';
-- how late Hollerau pays a supplier
SELECT AVG(julianday(paid_date) - julianday(due_date)) FROM invoices
WHERE supplier_id = 'sup:70210' AND discount_taken = 0 AND paid_date IS NOT NULL;
```
