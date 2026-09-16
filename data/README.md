# Data provenance

Raw and processed data are gitignored. This file records exactly what was used.

## Primary: LOBSTER free sample (to be filled in at M1)

- Source: https://lobsterdata.com/info/DataSamples.php
- Ticker: AAPL (placeholder; confirm)
- Date: 2012-06-21
- Levels: 10
- Files: `<TICKER>_2012-06-21_34200000_57600000_message_10.csv`, `..._orderbook_10.csv`
- Price unit: 1e-4 USD; tick = 100 units = $0.01
- Time: seconds after midnight (float), converted to int64 ns since epoch, America/New_York
- Maker fee used: 0 bps (placeholder; record venue schedule and rationale here)
