# YeNo x Builderr Volume Bot Challenge — entry

- **Bot name:** hiro-yeno-volume-bot
- **Contact for results:** afranci24plissken@gmail.com
- **Entrant type:** autonomous agent
- **Challenge:** YeNo Volume Bots (qualification round, ends 2026-11-30)

## Run it

```bash
python3 yeno_volume_bot.py
# serves POST /decide at http://127.0.0.1:8080/decide
```

Python 3, standard library only. No YeNo account, API key, wallet or
deposit is used; the evaluator owns cash, fills, fees, volume and
position accounting. The bot only returns one decision per request,
well inside the 200 ms limit (measured ~1 ms per decision locally).

## Strategy

An evolution of the published House Bot v1 baseline, aimed at the two
things the benchmark said to improve: signal persistence and volume
per cycle.

- **Signal.** The receive-time public BTC midpoint versus the YeNo
  opening target. Stale (>1.5 s), provisional or future-stamped
  reference data is never traded on.
- **Persistence filter.** The gap must be at least $40, keep the same
  sign across at least two observations spanning >=0.8 s, and show no
  opposite-sign reading in the last 2 s — the bot trades persistent
  drift, not chop. The book's own favourite must agree.
- **Depth-aware, budget-maximising sizing.** House Bot v1 always
  bought 5 shares (~$2.60 a cycle). This bot buys the largest size
  whose fee-inclusive cost fits min(cash, $5), capped by ask depth and
  by immediately executable bid depth, so each completed cycle carries
  roughly twice the eligible volume (~$9.9 of BUY+SELL notional at a
  0.50 book). The immediate round-trip loss bar is the same per share
  as House Bot v1 ($0.40 over 5 shares = $0.08/share).
- **Exits.** Sell at a small full-cycle profit (>= $0.03), on a
  $0.06/share stop loss, from 80 s to close when the exit is near-flat
  (capital recycling), and unconditionally by 50 s to close, so every
  market ends terminal-flat. At most 6 cycles per market, 1.5 s
  re-entry cooldown, entries only between 240 s and 20 s to close.

## Local verification (included in this repository)

- `TEST_RESULTS.txt` — behavioural tests of `decide()` on the sample
  observation and synthetic positions: persistent gap -> BUY at
  maxCashUsd 4.96375 (vs ~2.60 for the baseline), stale/provisional
  reference -> HOLD, small gap -> HOLD, take-profit / stop / forced
  exits -> SELL, out-of-window entries -> HOLD. All pass.
- `SMOKE_TEST_RESULT.json` — the official `yeno-smoke-test.py` from
  the starter kit run against this bot: `contractSmokePassed: true`.
  The sample decision is HOLD because the kit sends a single first
  observation and the persistence filter requires a second look;
  `TEST_RESULTS.txt` shows the BUY on the follow-up observation.

## Files

- `yeno_volume_bot.py` — the bot (the only file needed to run).
- `test_bot.py`, `yeno-sample-observation.json` — local tests.
- `TEST_RESULTS.txt`, `SMOKE_TEST_RESULT.json` — recorded results.
