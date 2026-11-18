# Tuning notes — btc-race-24h-20261118-1444

Dev window: 1,440 one-minute bars, 17 Nov 21:44 → 18 Nov 21:44 WIB.
A down day (buy-and-hold −2.80%, max drawdown −3.24%). One big V in the
morning (bottom ~bar 290 at $63,624 → peak bar 508 at $65,189, +245 bps);
the afternoon is flat with at most +77 bps recoveries. Oracle ceiling
(perfect foresight, 26 bps round-trip cost): ~$59.60 in 6 trades.

## Measured rounds (offline simulator, exact RULES.txt fill model)

1. Baseline (6h rolling high, dip 70/140, TP 120, no max-hold):
   **−$13.58, −1.36%, 5 trades, DD 2.77%** — the second position was held
   into the downtrend and never recovered.
2. Grid 6,912 combos (dip/TP/stop/regime/max-hold): best **+$3.70, 0.37%,
   7 trades, DD 1.28%**. The 360-bar warmup missed the morning V entirely.
3. Shorter rolling-high window + trailing exit (2,304 combos):
   best **+$19.77, 1.98%, 5 trades, DD 0.64%** (hi 240, dip 130/220,
   TP 200, max-hold 300).
4. Tiered take-profit (deep-dip entry → TP 220, shallow-dip entry → TP 60;
   243 combos): best **+$22.93, 2.29%, 5 trades, DD 0.64%**.
   Fixed a real bug found in this round: `apply_fills` reset `entry_dip`
   when the first buy filled, wiping the value `decide()` had recorded,
   so deep entries wrongly took the shallow TP.
5. Regime lookback fixed to 360 bars (was wrongly tied to the rolling-high
   window, which made hi_bars=180 score $7.08). Focused grid (486 combos)
   + neighbourhood sweep: plateau **$23.33–$23.86** across hi 180–240,
   TP-deep 210–220, size1 70–100, reclaim 15–20, max-hold 360–480.
   Cliffs avoided: TP-deep 230 (misses the peak by a hair on some entries),
   hi_bars ≥270 (enters too late, at a worse price), dip1 ≤110 (entries
   land shallow, take the small TP).

## Final parameters

hi_bars 210, dip1 130, dip2 220, size1 80%, reclaim 20, stable_bars 3,
tp_deep 220, tp_shallow 60, dip_deep 180, stop 250, regime −200 (6h),
max_hold 420, trail off, max_slippage 300.

**Final dev grade: +$23.80 (2.38%), 5 trades, max DD 0.64%, fees $1.46.**
Neighbours of the chosen point all profit ($19.85–$23.88), so the point
sits inside a robust region, not on a magic peak. It is $1.20 short of
the $25 target set against the previous race's top-1 (2.42% on that
race's own window): this dev window simply offers less — after the
morning V the afternoon's best recovery is +77 bps, and a round trip
costs ~26 bps.
