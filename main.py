#!/usr/bin/env python3
"""Stabilised dip-buy bot for cbBTC (CBBTC/USDC) on 1-minute bars.

Protocol: agent-fun/bars-replay/v1. Reads hello, one batch per minute, then
done on stdin; answers every batch with exactly one reply echoing its seq.

Idea (from the brief, written fresh here): buy AFTER a dip has stopped
falling, not while it falls. A dip is measured against the trailing
rolling high. An entry only fires once the price has stabilised -- it has
reclaimed a few bps off the dip low, or it has gone several bars without a
new low and ticked up. Entries come in two tranches (moderate dip, then a
deeper dip), each stabilised. A regime filter skips entries while the
trailing 6-hour return is deeply negative. Exits are a take-profit toward
the rolling high, a trailing exit once the position is in profit, a wide
disaster stop, a max-hold exit for stale positions, or holding to the end
of the window.

Only the standard library is used. No wall clock, no network, no files, no
unseeded randomness: the same input always produces the same orders.
"""

import json
import sys

# ---------------------------------------------------------------------------
# Strategy parameters (tuned on the dev window; see the tuning notes).
# All thresholds are in basis points (1 bp = 0.01%).
# ---------------------------------------------------------------------------
PARAMS = {
    "hi_bars": 210,          # rolling-high window, bars (3.5h)
    "dip_start_bps": 20,     # a dip episode starts once the fall passes this
    "dip1_bps": 130,         # first tranche: dip depth that arms the buy
    "dip2_bps": 220,         # second tranche: deeper dip depth
    "reclaim_bps": 20,       # stabilisation: reclaim this far above the dip low
    "stable_bars": 3,        # stabilisation: bars without a new low, then uptick
    "tp_deep_bps": 220,      # take profit after a deep-dip entry
    "tp_shallow_bps": 60,    # take profit after a shallow-dip entry
    "dip_deep_bps": 180,     # an entry dip at least this deep counts as deep
    "stop_bps": 250,         # disaster stop below the average entry price
    "regime_bps": -200,      # skip entries while the 6h return is below this
    "size1_frac_num": 80,    # first tranche: share of cash, in percent
    "size1_frac_den": 100,
    "no_entry_after": 1380,  # do not open new entries after this bar index
    "max_hold_bars": 420,    # exit a stale position after this many bars (0 = off)
    "trail_bps": 0,          # trailing exit: sell this far below the high since entry (0 = off)
    "trail_min_gain_bps": 50,  # trailing exit only once the gain reaches this
    "max_slippage_bps": 300,  # per-order slippage guard vs the latest close
}

SYMBOL = "CBBTC"
QUOTE_SCALE = 6          # quote amounts are integer micro-USDC
PRICE_SCALE = 5          # bar prices are integers of 1e-5 USD
QTY_SCALE = 8            # quantities are integers of 1e-8 BTC
MICRO_PER_UNIT = 10 ** (QTY_SCALE - PRICE_SCALE + QUOTE_SCALE)
# value in micro-USDC of qty Q at price P (price scale 5) is Q * P / 1e7.


def bps_of(numerator, denominator):
    """(numerator / denominator - 1) in basis points, integer math."""
    if denominator <= 0:
        return 0
    return (numerator - denominator) * 10000 // denominator


class DipBuyBot:
    """One bot instance. Pure bar-by-bar logic; no I/O inside decide()."""

    def __init__(self, params, capital_units, fee_bps, min_notional,
                 max_orders, warmup_bars):
        self.p = dict(params)
        self.capital = capital_units
        self.fee_bps = fee_bps
        self.min_notional = min_notional
        self.max_orders = max_orders
        self.warmup_bars = warmup_bars or 0

        self.closes = []          # close prices (int, price scale 5)
        self.bar_index = -1       # index of the latest bar seen
        self.cash = capital_units
        self.qty = 0              # position quantity (int, qty scale 8)
        self.entry_cost = 0       # micro-USDC paid for the position, ex-fee
        self.tranches = 0         # 0 = flat, 1 = first tranche held, 2 = both
        self.dip_low = 0          # lowest close of the current dip episode
        self.dip_low_bar = -1
        self.in_dip = False
        self.entry_bar = -1       # bar index where the position was opened
        self.entry_dip = 0        # dip (bps below rolling high) at first entry
        self.high_since_entry = 0  # highest close since the position opened
        self.order_no = 0

    # -- state helpers ----------------------------------------------------

    def avg_entry_price(self):
        """Average fill price of the open position (int, price scale 5)."""
        if self.qty <= 0:
            return 0
        return self.entry_cost * MICRO_PER_UNIT // self.qty

    def rolling_high(self):
        window = self.closes[-self.p["hi_bars"]:]
        return max(window) if window else 0

    def regime_ok(self):
        """True unless the trailing 6h return is deeply negative.

        The lookback is fixed at 360 bars (6h), independent of the
        rolling-high window: a shorter high window must not turn the
        regime filter into a short-horizon block.
        """
        n = 360
        if len(self.closes) <= n:
            return True
        old = self.closes[-n - 1]
        now = self.closes[-1]
        return bps_of(now, old) >= self.p["regime_bps"]

    def stabilised(self):
        """True once the dip has stopped falling.

        Either the price reclaimed `reclaim_bps` above the dip low, or it
        went `stable_bars` bars without a new low and closed above the
        previous bar's close.
        """
        if not self.in_dip or self.dip_low <= 0:
            return False
        close = self.closes[-1]
        if bps_of(close, self.dip_low) >= self.p["reclaim_bps"]:
            return True
        bars_since_low = self.bar_index - self.dip_low_bar
        if bars_since_low >= self.p["stable_bars"] and len(self.closes) >= 2:
            if close > self.closes[-2]:
                return True
        return False

    # -- per-batch bookkeeping ---------------------------------------------

    def apply_fills(self, fills):
        """Update cash/qty/cost from the fills the harness reports."""
        for fill in fills or []:
            if fill.get("status") != "filled":
                continue
            if fill.get("symbol") != SYMBOL:
                continue
            price = int(fill.get("price", "0"))
            qty = int(fill.get("qty", "0"))
            notional = int(fill.get("notional_quote_units", "0"))
            fee = int(fill.get("fee_quote_units", "0"))
            if fill.get("side") == "buy":
                if self.qty == 0:
                    # First fill of a new position: entry bar is the fill bar.
                    # entry_dip is NOT reset here: decide() recorded the dip
                    # that armed the entry, and the fill must not wipe it.
                    self.entry_bar = self.bar_index
                    self.high_since_entry = 0
                self.cash -= notional + fee
                self.qty += qty
                # cost basis at the fill price, before fees.
                self.entry_cost += qty * price // MICRO_PER_UNIT
            elif fill.get("side") == "sell":
                self.cash += notional - fee
                if self.qty > 0:
                    sold = min(qty, self.qty)
                    self.entry_cost -= self.entry_cost * sold // self.qty
                    self.qty -= sold
                if self.qty == 0:
                    self.entry_cost = 0
                    self.tranches = 0
                    self.entry_bar = -1
                    self.entry_dip = 0
                    self.high_since_entry = 0

    def apply_account(self, account):
        """Trust the harness account as the source of truth for cash/qty."""
        if not account:
            return
        cash = int(account.get("cash_quote_units", "0"))
        self.cash = cash
        qty = 0
        for pos in account.get("positions", []) or []:
            if pos.get("symbol") == SYMBOL:
                qty = int(pos.get("qty", "0"))
        if qty == 0 and self.qty != 0:
            self.qty = 0
            self.entry_cost = 0
            self.tranches = 0
            self.entry_bar = -1
            self.entry_dip = 0
            self.high_since_entry = 0
        elif qty != self.qty:
            # Position changed without a fill we saw (should not happen);
            # rescale the cost basis proportionally.
            if self.qty > 0:
                self.entry_cost = self.entry_cost * qty // self.qty
            self.qty = qty

    def track_dip(self):
        """Maintain the current dip episode against the rolling high."""
        close = self.closes[-1]
        if self.qty > 0 and self.entry_bar >= 0:
            if close > self.high_since_entry:
                self.high_since_entry = close
        high = self.rolling_high()
        if high <= 0:
            return
        dip = (high - close) * 10000 // high
        if not self.in_dip:
            if dip >= self.p["dip_start_bps"]:
                self.in_dip = True
                self.dip_low = close
                self.dip_low_bar = self.bar_index
        else:
            if close < self.dip_low:
                self.dip_low = close
                self.dip_low_bar = self.bar_index
            if dip <= 5:
                # Recovered to within 5 bps of the rolling high: episode over.
                self.in_dip = False
                self.dip_low = 0
                self.dip_low_bar = -1

    # -- decisions ----------------------------------------------------------

    def decide(self):
        """Return the list of orders for the current batch (may be empty)."""
        p = self.p
        orders = []
        if len(self.closes) < p["hi_bars"]:
            return orders
        if self.bar_index < self.warmup_bars:
            return orders

        close = self.closes[-1]
        high = self.rolling_high()
        if high <= 0:
            return orders
        dip = (high - close) * 10000 // high

        # Exits first: take profit (tiered by how deep the entry dip was),
        # the disaster stop, a stale position, or a trailing exit once the
        # position is in profit.
        if self.qty > 0:
            entry = self.avg_entry_price()
            if entry > 0:
                gain = bps_of(close, entry)
                if self.entry_dip >= p["dip_deep_bps"]:
                    tp = p["tp_deep_bps"]
                else:
                    tp = p["tp_shallow_bps"]
                stale = (p["max_hold_bars"] > 0 and self.entry_bar >= 0
                         and self.bar_index - self.entry_bar >= p["max_hold_bars"])
                trail = False
                if (p["trail_bps"] > 0 and self.high_since_entry > 0
                        and gain >= p["trail_min_gain_bps"]):
                    fall = (self.high_since_entry - close) * 10000 // self.high_since_entry
                    trail = fall >= p["trail_bps"]
                if gain >= tp or gain <= -p["stop_bps"] or stale or trail:
                    orders.append(self._sell_all())
                    return orders

        # Entries: only while flat or holding just the first tranche.
        if self.tranches >= 2:
            return orders
        if self.bar_index > p["no_entry_after"]:
            return orders
        if not self.regime_ok():
            return orders
        if not self.stabilised():
            return orders

        if self.tranches == 0 and dip >= p["dip1_bps"]:
            notional = self.cash * p["size1_frac_num"] // p["size1_frac_den"]
            if notional >= self.min_notional and notional <= self.cash:
                orders.append(self._buy(notional))
                self.tranches = 1
                self.entry_dip = dip
        elif self.tranches == 1 and dip >= p["dip2_bps"]:
            notional = self.cash
            if notional >= self.min_notional:
                orders.append(self._buy(notional))
                self.tranches = 2
        return orders

    def _buy(self, notional):
        self.order_no += 1
        return {
            "side": "buy",
            "client_id": "b%d-%d" % (self.bar_index, self.order_no),
            "symbol": SYMBOL,
            "notional_quote_units": str(int(notional)),
            "max_slippage_bps": self.p["max_slippage_bps"],
        }

    def _sell_all(self):
        self.order_no += 1
        return {
            "side": "sell",
            "client_id": "s%d-%d" % (self.bar_index, self.order_no),
            "symbol": SYMBOL,
            "qty": str(int(self.qty)),
            "max_slippage_bps": self.p["max_slippage_bps"],
        }

    # -- protocol -----------------------------------------------------------

    def on_batch(self, batch):
        bars = batch.get("bars", []) or []
        for bar in bars:
            if bar.get("symbol") != SYMBOL:
                continue
            self.closes.append(int(bar["c"]))
            self.bar_index += 1
        self.apply_fills(batch.get("fills", []) or [])
        self.apply_account(batch.get("account"))
        self.track_dip()
        return self.decide()


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "hello":
            capital = msg.get("capital", {}) or {}
            caps = msg.get("caps", {}) or {}
            fees = msg.get("fees", {}) or {}
            bot = DipBuyBot(
                PARAMS,
                int(capital.get("quote_units", "1000000000")),
                int(fees.get("fee_bps", 4)),
                int(fees.get("min_notional_quote_units", "1000000")),
                int(caps.get("max_orders_per_bar", 16)),
                int(msg.get("warmup_bars", 0) or 0),
            )
        elif kind == "batch":
            if bot is None:
                bot = DipBuyBot(PARAMS, 1000000000, 4, 1000000, 16, 0)
            orders = bot.on_batch(msg)
            reply = {"type": "reply", "seq": msg.get("seq", 0),
                     "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
