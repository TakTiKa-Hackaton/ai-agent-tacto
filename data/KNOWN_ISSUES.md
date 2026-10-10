# Known issues

The data is shaped like a real ERP extract, quirks included. None of these are errors to report.

1. Three delivery dates per PO line: requested, first confirmed and the goods-receipt posting date. OTD
   differs by 10-30 points depending on the reference date.
2. Goods receipts are posted on working days only (weekends, Bavarian holidays, August shutdown).
3. Partial deliveries (two receipts per line) and small over-deliveries within 5 %.
4. C-parts, some raw material and the motor cable are priced per 100 or per 1,000 units (`price_unit`);
   a PO line's value is qty x price / price_unit.
5. One supplier sits twice in the vendor master: `sup:71999` is the old record of `sup:70210` (renamed 2024).
6. A few PO lines use an outdated price (`price_note = stale_condition`): purchase price variance.
7. Index clauses are in `contract_clauses.csv` and in the contract text (`contracts/`).
8. Past quotes miss fields where the supplier left them out (`missing_fields`).
9. Invoices are paid late for some suppliers; a few cash discounts were taken after the discount period.
10. Complaints on small lots make PPM jumpy; read the rolling scorecard, not single lots.
11. PO lines with requested dates after 2026-09-30 are open orders without receipts.
12. Index series end at different months: each source publishes with its own lag (`index_series.csv`).
13. Small suppliers file no profit and loss account: `supplier_intel.csv` gives their turnover as a band.
14. Should-cost (`should_cost.csv`) assumes a German cost base and has an error of 5-15 %.
15. The spend cube holds about 400 further suppliers known only by name, category and spend.
