# Data provenance

Raw and processed data are gitignored. This file records exactly what was used.

## Primary: LOBSTER free sample, AAPL, 2012-06-21, 10 levels

- Source: https://lobsterdata.com/info/DataSamples.php (free sample files, ReadMe version 01 Sept 2013)
- Files in `data/raw/`:
  - `AAPL_2012-06-21_34200000_57600000_message_10.csv` (400,391 rows)
  - `AAPL_2012-06-21_34200000_57600000_orderbook_10.csv` (400,391 rows)
  - `LOBSTER_SampleFiles_ReadMe.txt`
- Session: 09:30:00 to 16:00:00 America/New_York (EDT, UTC-4). Midnight = 1340251200000000000 ns.
- Price unit: 1e-4 USD. Tick = 100 units = $0.01. Prices stored as integer ticks (`price // 100`).
- Time: seconds after midnight, up to 9 decimals; parsed as text and converted exactly to int64 ns.
- Message types present: 1 (191,015), 2 (3,260), 3 (171,126), 4 (23,658), 5 (11,332). No halts (7) or crosses (6).
- Canonical stream: 634,638 events = 600,020 LEVEL_SET + 34,618 TRADE.
- Dropped: 372 hidden executions (type 5) at sub-penny midpoint prices, which cannot sit on the tick grid.
- Replay validation (`scripts/validate_replay.py`): 0 mismatches at all 10 levels over 400,391 rows.
- Known artefact: only 10 levels are visible, so a level scrolling out of the window is emitted as `qty = 0`.
  Top-of-book and queue-ahead at our quotes are unaffected; depth beyond level 10 is not modelled.

### Fees

Maker fee used: **0 bps**. NASDAQ's 2012 schedule paid displayed liquidity providers a rebate
(about $0.0020 to $0.0029 per share) and charged takers about $0.0030 per share. We never take,
and we ignore the rebate so that reported PnL is not flattered by it. Fees remain wired through the
engine (E5 toggles them) so a crypto-style maker fee can be substituted with one config change.

## Secondary (not used): LOBSTER MSFT, same day

`MSFT_2012-06-21_34200000_57600000_{message,orderbook}_10.csv` are also in `data/raw/`.
Mean spread is ~1.4 ticks, so inventory skew cannot be expressed in ticks; kept only as an
optional robustness run.
