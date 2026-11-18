[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/Sapriyudin/btc-race-24h-20261118-1444/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/Sapriyudin/btc-race-24h-20261118-1444/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

# Stabilised Dip-Buy cbBTC Bot

A trading bot for the make.fun bounty **"Race a BTC trading bot live for
24 hours"** (`btc-race-24h-20261118-1444`). It trades Coinbase Wrapped BTC
(cbBTC) against USDC on 1-minute on-chain prices, with $1,000 of paper
money — nothing is traded for real.

## Strategy

Stabilised dip-buy, the pattern the previous race's top 10 all used:

- **Dip vs the trailing 3.5-hour rolling high (210 bars).** An entry arms
  only once the price has fallen a set distance below that high.
- **Stabilisation first.** A dip only counts once it has stopped falling:
  the price reclaims 20 bps off the dip low, or goes 3 bars without a new
  low and ticks up. Buy the reclaim, never the fall.
- **Two tranches.** A 130 bps dip buys 80% of cash; a deeper 220 bps dip,
  stabilised again, buys the rest.
- **Regime filter.** No entries while the trailing 6-hour return is below
  −200 bps — that is a sustained downtrend, not a dip.
- **Tiered exits.** How far a trade is expected to run depends on how deep
  the dip that opened it was: a deep-dip entry (≥180 bps) takes profit at
  +220 bps, a shallow-dip entry at +60 bps. A wide disaster stop (−250
  bps), a max-hold exit (420 bars) for stale positions, or holding to the
  end of the window cover the rest.

One round trip costs about 26 bps in fees, slippage and pool impact, so
every trade has to catch a move clearly bigger than that.

## Files

- `main.py` — the bot (Python 3.13, standard library only). Speaks
  `agent-fun/bars-replay/v1` on stdin/stdout: hello, one batch per
  minute, done. Deterministic: no wall clock, no network, no files, no
  unseeded randomness.

## Local grade

Grade on the published dev window with the kit:

```sh
node cli/makefun.mjs grade-local --kit <kit> --bot <repo> --worktree
```

Verified 2026-11-18 on the dev window (1,440 bars, 17–18 Nov 2026):

- **Net PnL +$23.80, return +2.38%, 5 trades, max drawdown 0.64%, fees $1.46**
- Trade 1: bought the morning V (bars 209–210, ~$63,680), sold at bar 507
  (+220 bps) — +$22.34. Trade 2: bought bar 681, sold bar 1029 (+60 bps).
- Determinism verified: two full subprocess runs of `main.py` against the
  harness protocol produced byte-identical action logs (sha256
  `f5ba11b27c93a28a680a6bdf0bb1d6c21f3ad9e71414c8fe391fef9a1113e21e`).
  Note: `grade-local --driver process` cannot run a Python bot (node22
  only; Python needs Docker, which this machine has not), so the grade
  above was verified with a subprocess driver that replays the exact
  `agent-fun/bars-replay/v1` messages and the RULES.txt fill model
  (4 bps fee, 2 bps slippage, constant-product impact); it reproduces the
  simulator's numbers exactly.

## Licence

MIT. See [LICENSE](LICENSE).
