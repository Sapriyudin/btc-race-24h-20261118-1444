#!/usr/bin/env python3
"""Stabilised dip-buy cbBTC bot, v2.

Trades Coinbase Wrapped BTC (CBBTC) against USDC, one-minute bars, long
only, at most one open position. Protocol: agent-fun/bars-replay/v1,
newline-delimited JSON on stdin/stdout. One reply per batch, echoing seq.
Orders decided on a batch fill at the next bar's open.

Strategy (all integer math, no floats, no wall clock, no randomness):
- Rolling high: the highest close of the trailing 210 bars before the
  current bar. A dip is how far the current close sits below that high,
  in basis points.
- Warmup: no entries before bar 210, so the rolling high is real.
- Tranche 1: when the dip reaches 150 bps, watch the dip low (the lowest
  close since the dip crossed 150). Enter when the price stops making new
  lows -- the first bar whose close is not below the previous close --
  with 90% of cash.
- Tranche 2: while holding only tranche 1, if the dip deepens to 220 bps,
  watch the same way and add the remaining cash (10% of the position's
  opening capital, capped by cash left) once the price stops falling.
- Regime filter: skip entries while the trailing 6-hour return is below
  -200 bps (only checked once 360 bars of history exist).
- Exits, checked every bar while holding: take profit at +220 bps above
  the deepest tranche's entry price when a deep tranche is held, else
  +80 bps above the first entry price; disaster stop at -250 bps below
  the position's average entry price; max hold 420 bars. Sell the whole
  position in one order.
- Orders carry max_slippage_bps 200. The widest open-vs-previous-close
  gap on the dev window is 18 bps, so fills are effectively never
  rejected for slippage.

Tuned on the frozen dev window by grid search (see TUNING.md). The
shallow-dip tier and the trend-ride mode from the v2 brief were built,
measured and dropped: both lose money on this window (the shallow tier's
extra trades all sit in the afternoon grind, and the trend mode fires on
false breakouts in the chop). This file is the dip-buy core only.
"""

import json
import sys

SYMBOL = "CBBTC"

# --- tuned constants (see TUNING.md for the grid) ---
ROLLING_HIGH_BARS = 210
DIP_1_BPS = 150
DIP_2_BPS = 220
TRANCHE_1_NUM = 90  # 90% of cash
TRANCHE_2_NUM = 10  # 10% of the position's opening capital
TP_SHALLOW_BPS = 80
TP_DEEP_BPS = 220
STOP_BPS = 250
MAX_HOLD_BARS = 420
REGIME_LOOKBACK = 360
REGIME_MIN_BPS = -200
MAX_SLIPPAGE_BPS = 200


def reply(seq, orders):
    sys.stdout.write(json.dumps(
        {"type": "reply", "seq": seq, "orders": orders},
        separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    closes = []          # closes seen so far, ints
    tranches = []        # (qty, entry_price, tag) from buy fills
    pos_tp = None        # take-profit bps for this position
    pos_stop = None      # stop bps for this position
    pos_hold = None      # max-hold bars for this position
    pos_bar0 = None      # bar the position's first buy was decided on
    pos_capital = None   # equity when the position opened
    dip_low = None       # lowest close of the current dip-1 episode
    dip2_low = None      # lowest close of the current dip-2 episode
    pending_buy = {}     # client_id -> tag, buys sent, fill not yet seen
    pending_sell = None  # client_id of a sell sent, fill not yet seen
    counter = 0          # order counter (deterministic client ids)

    def next_cid(kind):
        nonlocal counter
        counter += 1
        return "%s%d" % (kind, counter)

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mtype = msg.get("type")
        if mtype == "hello":
            continue  # hello gets no reply
        if mtype == "done":
            break
        if mtype != "batch":
            continue
        seq = msg.get("seq", 0)

        # --- fills reported with this batch: update state first ---
        for fill in msg.get("fills", []):
            cid = fill.get("client_id", "")
            side = fill.get("side")
            status = fill.get("status")
            if cid in pending_buy:
                tag = pending_buy.pop(cid)
                if side == "buy" and status == "filled":
                    tranches.append((int(fill.get("qty", "0")),
                                     int(fill.get("price", "0")),
                                     tag))
            elif cid == pending_sell:
                # a sell fill (or rejection) clears the pending sell;
                # the account below shows whether the position is gone
                pending_sell = None

        # --- account as reported with this batch ---
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", "0"))
        positions = account.get("positions", [])
        qty = int(positions[0].get("qty", "0")) if positions else 0
        cost = int(positions[0].get("cost_quote_units", "0")) if positions else 0
        if qty == 0:
            tranches = []
            pos_tp = pos_stop = pos_hold = pos_bar0 = None
            pos_capital = None

        # --- the bar that just closed ---
        bars = msg.get("bars", [])
        bar = None
        for b in bars:
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        if bar is None:
            reply(seq, [])
            continue
        close = int(bar["c"])
        closes.append(close)
        i = len(closes) - 1

        # --- rolling high over the trailing window before this bar ---
        start = i - ROLLING_HIGH_BARS
        if start < 0:
            start = 0
        window = closes[start:i]
        high = max(window) if window else close
        dip = (high - close) * 10000 // high if high else 0

        # --- regime filter: trailing 6h return ---
        regime_bad = False
        if i >= REGIME_LOOKBACK:
            ref = closes[i - REGIME_LOOKBACK]
            regime_bad = (close - ref) * 10000 // ref < REGIME_MIN_BPS

        has_pos = qty > 0
        has_deep = any(t[2] == "deep" for t in tranches)
        prev_close = closes[i - 1] if i >= 1 else close

        # stabilisation, judged on the dip-1 episode low as of the
        # previous bar: the price has stopped making new lows
        stab1 = dip_low is not None and close >= prev_close

        orders = []

        # --- exits: only while holding and no sell is outstanding ---
        if has_pos and pending_sell is None:
            avg_entry = cost * 10 ** 7 // qty if qty else close
            if has_deep:
                tp_ref = min(t[1] for t in tranches if t[2] == "deep")
                take_profit = TP_DEEP_BPS
            else:
                tp_ref = tranches[0][1]
                take_profit = pos_tp
            sell = False
            if close * 10000 >= tp_ref * (10000 + take_profit):
                sell = True
            elif close * 10000 <= avg_entry * (10000 - pos_stop):
                sell = True
            elif pos_bar0 is not None and i - pos_bar0 >= pos_hold:
                sell = True
            if sell:
                cid = next_cid("s")
                pending_sell = cid
                orders.append({"side": "sell", "client_id": cid,
                               "symbol": SYMBOL, "qty": str(qty),
                               "max_slippage_bps": MAX_SLIPPAGE_BPS})
                dip_low = None
                dip2_low = None
                reply(seq, orders)
                continue

        # --- entries: only while flat, no buy outstanding, regime ok ---
        if (not has_pos and not pending_buy and not regime_bad
                and i >= ROLLING_HIGH_BARS):
            if dip >= DIP_1_BPS:
                if dip_low is None:
                    dip_low = close
                elif close < dip_low:
                    dip_low = close
                if stab1:
                    notional = cash * TRANCHE_1_NUM // 100
                    if notional >= 1000000:
                        cid = next_cid("b")
                        pending_buy[cid] = "shallow"
                        orders.append({"side": "buy", "client_id": cid,
                                       "symbol": SYMBOL,
                                       "notional_quote_units": str(notional),
                                       "max_slippage_bps": MAX_SLIPPAGE_BPS})
                        pos_tp = TP_SHALLOW_BPS
                        pos_stop = STOP_BPS
                        pos_hold = MAX_HOLD_BARS
                        pos_bar0 = i
                        pos_capital = cash + qty * close // 10 ** 7
                    dip_low = None
                    reply(seq, orders)
                    continue
            else:
                dip_low = None

        # --- tranche 2: while holding only tranche 1 ---
        if has_pos and not has_deep and not pending_buy and not regime_bad:
            if dip >= DIP_2_BPS:
                if dip2_low is None:
                    dip2_low = close
                elif close < dip2_low:
                    dip2_low = close
                # stabilisation judged on the low just updated: the price
                # has stopped making new lows
                if close >= prev_close:
                    base = pos_capital if pos_capital is not None else cash
                    notional = base * TRANCHE_2_NUM // 100
                    if notional > cash:
                        notional = cash
                    if notional >= 1000000:
                        cid = next_cid("b")
                        pending_buy[cid] = "deep"
                        orders.append({"side": "buy", "client_id": cid,
                                       "symbol": SYMBOL,
                                       "notional_quote_units": str(notional),
                                       "max_slippage_bps": MAX_SLIPPAGE_BPS})
                    dip2_low = None
            else:
                dip2_low = None

        reply(seq, orders)


if __name__ == "__main__":
    main()
