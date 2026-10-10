# Supplier scorecard - method

Each row (`sc:<supplier>@<quarter>`) covers the rolling 12 months to the end of that quarter and is sent to
the supplier, so both sides know it.

- **otd_pct**: share of goods-receipt lines posted between 5 days before and 2 days after the
  requested date of the PO line. **otd_confirmed_pct**: the same against the supplier's first confirmed date.
  Partial deliveries count as separate lines.
- **ppm**: supplier-caused complaint quantity / received quantity x 1,000,000 (complaints by date of the QN).
- **open_8d_overdue**: supplier-caused complaints older than 60 days without a closed 8D report at quarter end.
- **score** = 0.5 x otd_pct + 0.5 x quality points - 5 x open_8d_overdue, with quality points =
  100 x max(0, 1 - ppm / 10,000).
- **class / colour**: A green >= 85, B amber 70-84.9, C red < 70.
