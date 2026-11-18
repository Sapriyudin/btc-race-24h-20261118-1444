# TUNING.md — btc-race v2 (Stabilised Dip-Buy, upgraded core)

## Final parameters (main.py constants)

| Parameter | Value | Meaning |
|---|---|---|
| ROLLING_HIGH_BARS | 210 | trailing window for the rolling high (closes before current bar) |
| DIP_1_BPS | 150 | tranche-1 dip threshold vs rolling high |
| DIP_2_BPS | 220 | tranche-2 (add-on) dip threshold |
| TRANCHE_1 | 90% of cash | first buy size |
| TRANCHE_2 | 10% of opening capital, capped by cash left | add-on size |
| TP_SHALLOW_BPS | 80 | take-profit when only tranche 1 is held (vs first entry price) |
| TP_DEEP_BPS | 220 | take-profit when a deep tranche is held (vs deep entry price) |
| STOP_BPS | 250 | disaster stop vs average entry price |
| MAX_HOLD_BARS | 420 | max bars held |
| REGIME_MIN_BPS | -200 | skip entries while trailing 6h return < -200 bps |
| MAX_SLIPPAGE_BPS | 200 | per-order slippage cap (widest open-vs-close gap on dev: 18 bps) |
| Warmup | 210 bars | no entries before bar 210 |
| Stabilisation | "nonlower" | enter on the first bar whose close is not below the previous close, after the dip low is set |

## Local dev grade (driver /tmp/btcrace/drive.py, exact protocol + fill model)

- **Net PnL +$25.68, return +2.57%, 5 trades, max drawdown 0.64%, fees $1.54**
- Determinism: two driver runs produce byte-identical action logs
  (sha256 `0f5391f6ed6e976ab97c59e94d353546d6152fa9c2a36b9abe4cfa30797c1202`).
- Trades: buy $900 @ bar 212 (fill $63,734.69) + buy $99.64 @ bar 213
  (fill $63,664.72), sell all @ bar 507 (fill $65,061.82); buy $917.74
  @ bar 679 (fill $64,247.19), sell all @ bar 1029 (fill $64,742.37).

## Baseline: v1 core reproduced first

Reimplementing the v1 prose (210-bar high, dips 130/220, 80/20 split,
TP 60/220, stop -250, hold 420, regime -200) in the same simulator
gives **+$23.06, 5 trades, maxDD 0.64%, fees $1.46** — close to v1's
reported server dev grade (+$23.80; the sim runs ~$0.74 below the server
on v1, so the server grade for v2 may land a little above +$25.68).
Three stabilisation rules were tested; only "nonlower" (first bar that
does not make a new low) reproduces v1's trade shape (buys 212/213,
sell 507). The "reclaim +20 bps from the dip low" rule fires too late
(buy 216, deep tranche never arms, +$8.03) and "reclaim +5 bps" is
worse (+$5.87).

## Grid searched (simulator mirroring drive.py fill math exactly)

- Core W/D1/D2: W {120,150,180,210,240,300}, D1 {90,110,130,150,170},
  D2 {180,200,220,240,260}. Best region W=210, D1=130–150. D1=90 is
  disastrous (-$13): too many false entries. D2 barely matters
  (180/200/220 identical) except 240, which breaks trade 1.
- TPs/STOP/HOLD around the best core: TP_SHALLOW {40..120},
  TP_DEEP {180..260}, STOP {200,250,300}, HOLD {300,420,600}.
  TP_SHALLOW 80 is best ($24.42 at S1=0.8); TP_DEEP 220–235 is a flat
  plateau, 240 is a cliff ($15.93 — the +240 target just misses the
  window peak $65,189.54). STOP and HOLD never bind on this window.
- Tranche split S1/S2: 0.80/0.20 $24.42, 0.85/0.15 $25.55,
  0.90/0.10 $25.68, 0.95/0.05 $25.77. Chosen 0.90/0.10: on the plateau,
  keeps a real second tranche (averages down if the dip deepens after
  entry); 0.95 gains only +$0.09 more and nearly kills the mechanism.
  S1=1.00 (no tranche 2) collapses to $13.86 because the exit then
  falls back to the shallow TP.

## Neighbour robustness (one parameter varied, rest = final)

All neighbours profit; every one makes 5 trades with maxDD <= 0.71%.

| Varied | PnL |
|---|---|
| D1 = 140 / 145 / **150** / 155 | $25.68 / $25.68 / **$25.68** / $25.68 |
| W = 200 / **210** / 220 | $23.96 / **$25.68** / $20.86 |
| TP_SHALLOW = 70 / **80** / 90 | $23.99 / **$25.68** / $25.68 |
| TP_DEEP = 210 / **220** / 230 | $23.80 / **$25.68** / $25.68 |
| S1 = 0.85 / **0.90** / 0.95 | $25.55 / **$25.68** / $25.77 |
| D2 = 180 / 200 / **220** | $25.68 / $25.68 / **$25.68** |
| STOP = 200 / **250** / 300 | $25.68 / **$25.68** / $25.68 |
| MAX_HOLD = **420** / 600 | **$25.68** / $25.68 |

## Additions tried and REJECTED (measured, not kept)

1. **Shallow-dip tier** (extra entries on 40–70 bps dips, small size,
   quick TP, with and without momentum gates, armed-episode logic):
   best case $21.93, usually $14–16 vs $24.42 core-only. Two reasons
   it fails on this window: (a) the two genuinely good shallow
   pullbacks (bars ~441 and ~841) happen WHILE the bot already holds a
   core position — the single-position rule makes them unreachable;
   (b) the shallow entries it can take (bar ~602, bars ~1184–1299) sit
   in the afternoon grind-down and lose money or block the good core
   trade at bar 679. NOT in main.py.
2. **Trend-ride mode** (enter on a break above the trailing high with
   rising 10-bar momentum, small tranche, trailing exit, hard stop):
   $19.29–$20.70 vs $24.42 core-only, 9 trades vs 5. The only real
   breakout (bars ~480–508, peak $65,190) offers ~14 bps above the old
   high — below round-trip costs — and every later "breakout" is a
   false high in the chop. NOT in main.py.

## Honest shortfall vs the >$30 target

v2 beats v1 (+$25.68 vs +$23.80 server / +$23.06 sim) but does NOT
reach +$30, and the gap is structural, not tuning: with one position
at a time, this window only holds two profitable round trips (the
morning V, ~+$20, and the afternoon pullback, ~+$4.50). The other
recoveries either overlap the core holdings (bars 441, 841) or never
rise above round-trip costs (~31 bps: 4 fee + 2 slippage + ~9.5 impact
each way) in the afternoon grind. TP_DEEP=238 squeezes +$0.67 more by
selling exactly at the window peak ($65,189.54) — rejected as
peak-fitting: it is a one-bar cliff (240 already collapses to $15.93).
No parameter set found in ~1,500 grid runs beats +$25.77 without
relying on the exact peak. The server grade is the authority; the
simulator has historically run ~$0.74 below the server on v1.
