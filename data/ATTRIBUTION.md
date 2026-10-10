# Attribution and licences

Market series fetched on 2026-10-08 and frozen at reference month 2026-09 (the latest month each source had published). Company data is synthetic; names of companies are fictional and any match with a real company is coincidental.

| Source | Licence | Series |
|---|---|---|
| Eurostat, `sts_inpp_m` (producer prices) - (c) European Union | reuse with acknowledgement (Eurostat copyright notice) | PPI-C2451, PPI-C2452, PPI-C2562, PPI-C2511, PPI-C2594, PPI-C2711, PPI-C2732, PPI-C2611, PPI-C2612, PPI-C2815, PPI-C241, PPI-C2431, PPI-C2219, PPI-C2813, PPI-D351, PPI-D35 |
| Eurostat, `lc_lci_r2_q` (labour cost index) - (c) European Union | reuse with acknowledgement (Eurostat copyright notice) | LCI-C |
| World Bank, Commodity Price Data (The Pink Sheet) | CC BY 4.0 | WB-CU, WB-NI, WB-AL, WB-NGEU, WB-FEORE |
| European Central Bank, reference exchange rates (EXR) | free reuse with source (ECB) | ECB-USD, ECB-PLN, ECB-CZK, ECB-HUF, ECB-TRY, ECB-CNY, ECB-CHF |
| Bundesnetzagentur \| SMARD.de, day-ahead prices | CC BY 4.0 | SMARD-DA |

Modifications: quarterly labour cost carried to its three months; derived series (our own arithmetic on the open series, not the publishers' figures):

- **CU-NOTATION** (EUR/100 kg): WB-CU / ECB-USD / 10 x 1.025 (delivery premium), month of the copper average
- **AS-14404** (EUR/t): (0.10 x Ni + 0.165 x 2500 + 0.02 x 50000) / 0.95; Ni = WB-NI / ECB-USD of the previous month; Cr and Mo held at fixed EUR/t (no open monthly series)
- **AS-14301** (EUR/t): (0.08 x Ni + 0.18 x 2500) / 0.95; Ni = WB-NI / ECB-USD of the previous month; Cr held at a fixed EUR/t

Source URLs: see `index_series.csv`.
