# Hollerau Pumpenwerke GmbH

Family-owned pump maker from Hollerau, Lower Bavaria, Germany, founded 1952, owned by the Stangl family (third generation). Revenue about
EUR 252 M, 910 employees, one plant. Fiscal year October to September.

## Products
- **HN**: HN standard water pumps (EN 733)
- **HB**: HB building-services inline pumps
- **HI**: HI industrial process pumps, stainless (ISO 5199)
- **HX**: HX high-efficiency series (start of production 2027-03)

## Purchasing
You are the purchasing team for the pump line's parts and materials (about EUR 35 M in FY2026, see
`spend_cube.csv`). The detailed data covers the 46 suppliers and 60 parts that matter most.

## Policies
- Standard payment terms: 60 days net. Cost of capital for valuing payment terms:
  8% a year.
- Approval: deals above EUR 250 k a year need the head of purchasing, above EUR 1 M the managing director.
- A-parts are dual-sourced where a qualified second source exists (`sourcing.csv`).
- Quality agreement with every series supplier: on-time delivery target 95 %, flat fee of
  EUR 150 per supplier-caused complaint, 8D report within 60 days.
- Preferred Incoterm: DAP Hollerau. Freight for EXW/FCA deliveries is booked by Hollerau.

## Should-cost
`should_cost.csv` estimates full cost without margin. For the parts of strategic and bottleneck
negotiations the category team modelled the current supplier's own plant (`supplier_id`, error about
5 %); the other rows use a German cost base with an error of 5-15 %. Rule of thumb for other countries'
full cost relative to Germany: AT and CH about 1.0-1.1, CZ, PL and HU about 0.8-0.85, TR about 0.75,
CN about 0.7.

## Category strategies
Every negotiation states its category goal (`stabilise`, `secure_supply`, `quality_first`, `leverage`, `routine`); the rates of its cost C follow it.
Quality escalations are `quality_first`, capacity squeezes `secure_supply`, kits and bundles `routine`.

| Category | Strategy | Goal |
|---|---|---|
| castings | secure supply and energy-cost transparency; build Central-European second sources | stabilise |
| machined parts | leverage: competition between local job shops, bundle small parts | leverage |
| sheet metal | leverage; quality first after the 2026 base-frame issues | leverage |
| seals | bottleneck: long-term agreements, qualify second sources where approvals allow | secure_supply |
| electrical | strategic: copper and electronics clauses, multi-year frameworks | stabilise |
| C-parts | routine: fewer suppliers, kits, lower process cost | routine |
| raw material | index-based pricing, negotiate base price and extras | leverage |

## Targets for FY2027
Material cost -3 % against the index-adjusted baseline; no supplier in red for more than two quarters.
