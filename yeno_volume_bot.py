#!/usr/bin/env python3
"""Volume bot for the YeNo x Builderr Volume Bot Challenge.

Strategy summary (an evolution of the published House Bot v1 baseline):

- Signal: the receive-time public BTC midpoint versus the YeNo opening
  target for the current five-minute market. A persistent gap means the
  book's favourite is likely to keep drifting the same way.
- Entry: buy the gap-favoured side only when the gap is at least
  MIN_GAP_USD, has kept the same sign across recent observations
  (persistence filter against chop), the book's own favourite agrees,
  the best ask is at most MAX_ENTRY_PRICE, and an immediate round trip
  would lose no more than MAX_IMMEDIATE_LOSS_PER_SHARE_USD per share
  (the same per-share bar House Bot v1 used: $0.40 over 5 shares).
- Sizing: depth-aware and budget-maximising. House Bot v1 always bought
  5 shares; this bot buys the largest size whose fee-inclusive cost fits
  inside min(cash, $5) and whose full size is also covered by immediate
  bid depth, so every cycle carries roughly twice the eligible volume.
- Exit: sell as soon as the full-cycle result is a small profit, on a
  per-share stop loss, near the close for capital recycling, and always
  early enough to guarantee a terminal-flat finish for the market.
- Safety: stale, provisional or future-stamped reference data is never
  traded on. Every rule from the evaluator contract (one position,
  $5 fee-inclusive BUY cap, no entries inside the final seconds) is
  respected by construction.

Only the Python standard library is used. The evaluator owns cash,
fills, fees, volume and position accounting; this program only returns
one decision per request.
"""

from __future__ import annotations

import json
import math
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --- Tunables -------------------------------------------------------------
MIN_GAP_USD = 40.0               # minimum |BTC mid - opening target|
PERSISTENCE_MIN_SPAN_S = 0.8     # gap sign must persist at least this long
PERSISTENCE_MIN_SAMPLES = 2      # ... across at least this many observations
MAX_ENTRY_PRICE = 0.92
MAX_IMMEDIATE_LOSS_PER_SHARE_USD = 0.08  # House Bot v1: $0.40 over 5 shares
MAX_REFERENCE_AGE_S = 1.5
ENTRY_MIN_SECONDS = 20.0         # never enter inside the final 20s
ENTRY_MAX_SECONDS = 240.0        # entries only in the first 4 minutes
FORCED_EXIT_SECONDS = 50.0       # always flat by here
RECYCLE_EXIT_SECONDS = 80.0      # near-flat exits allowed from here
TAKE_PROFIT_USD = 0.03           # sell at or above this full-cycle PnL
RECYCLE_MAX_LOSS_USD = 0.08      # recycle exit tolerates at most this loss
STOP_LOSS_PER_SHARE_USD = 0.06
MAX_CYCLES_PER_MARKET = 6
REENTRY_COOLDOWN_S = 1.5
SHARE_STEP = 0.5                 # sizing granularity


def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            output.append((price, size))
    return sorted(output, reverse=reverse)


def _fees(fills: list[tuple[float, float]]) -> float:
    gross = sum(price * shares for price, shares in fills)
    protocol = sum(0.07 * shares * price * (1 - price) for price, shares in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def _walk(book: Any, shares: float, *, buying: bool) -> tuple[float, float] | None:
    """(gross, fees) to trade `shares` against a book side, or None."""
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _levels(book, reverse=not buying):
        quantity = min(remaining, available)
        fills.append((price, quantity))
        remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return sum(price * quantity for price, quantity in fills), _fees(fills)


def _depth(book: Any, *, buying: bool) -> float:
    return sum(size for _price, size in _levels(book, reverse=not buying))


class VolumeBot:
    def __init__(self) -> None:
        self.market_id: str | None = None
        self.position_was_open = False
        self.completed_cycles = 0
        self.last_flat_at = -math.inf
        self.last_buy_attempt_at = -math.inf
        self.gap_history: deque[tuple[float, float]] = deque(maxlen=12)

    # -- main entry --------------------------------------------------------
    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._decide(observation)
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"action": "HOLD"}

    def _decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        market = observation["market"]
        account = observation["account"]
        books = observation["books"]
        now = float(observation["timestamp"])
        seconds = float(market["secondsToClose"])
        market_id = str(market["id"])
        position = account.get("position")

        if market_id != self.market_id:
            self.market_id = market_id
            self.position_was_open = False
            self.completed_cycles = 0
            self.last_flat_at = -math.inf
            self.last_buy_attempt_at = -math.inf
            self.gap_history.clear()

        if position is None and self.position_was_open:
            self.completed_cycles += 1
            self.last_flat_at = now
        self.position_was_open = position is not None

        # Track the reference gap for the persistence filter.
        gap = self._reference_gap(observation, now)

        if position is not None:
            return self._exit(observation, position, books, seconds)

        return self._enter(observation, account, books, now, seconds, gap)

    # -- helpers -----------------------------------------------------------
    def _reference_gap(self, observation: dict[str, Any], now: float) -> float | None:
        reference = observation.get("reference")
        if not isinstance(reference, dict):
            return None
        if bool(reference.get("targetProvisional", True)):
            return None
        if float(reference.get("targetObservedAt", math.inf)) > now + 1e-9:
            return None
        if now - float(reference.get("observedAt", -math.inf)) > MAX_REFERENCE_AGE_S:
            return None
        gap = float(reference["btcMidUsd"]) - float(reference["openingTargetUsd"])
        self.gap_history.append((now, gap))
        return gap

    def _gap_is_persistent(self, gap: float, now: float) -> bool:
        if abs(gap) < MIN_GAP_USD:
            return False
        sign = 1.0 if gap > 0 else -1.0
        samples = [(t, g) for (t, g) in self.gap_history if g * sign > 0]
        if len(samples) < PERSISTENCE_MIN_SAMPLES:
            return False
        span = now - min(t for t, _g in samples)
        recent = [g for (t, g) in self.gap_history if now - t <= 2.0]
        if recent and any(g * sign <= 0 for g in recent):
            return False
        return span >= PERSISTENCE_MIN_SPAN_S - 1e-9

    def _exit(self, observation: dict[str, Any], position: dict[str, Any],
              books: dict[str, Any], seconds: float) -> dict[str, Any]:
        if str(position.get("market_id", "")) not in ("", str(observation["market"]["id"])):
            return {"action": "HOLD"}
        side = str(position["outcome"])
        shares = float(position["shares"])
        book = books.get(side, {})
        executable = _walk(book.get("bids", []), shares, buying=False)
        if executable is None:
            # Not enough bid depth for the full size. Near the close a
            # partial exit is still better than settlement (which adds
            # zero eligible volume), so sell what the book can take.
            if seconds <= FORCED_EXIT_SECONDS + 1e-9 and _depth(book.get("bids", []), buying=False) > 0:
                return {"action": "SELL"}
            return {"action": "HOLD"}
        sell_gross, sell_fees = executable
        paid = float(position["buy_gross_usd"]) + float(position["buy_fees_usd"])
        received = float(position.get("sell_gross_usd", 0)) - float(position.get("sell_fees_usd", 0))
        pnl = received + sell_gross - sell_fees - paid
        if pnl >= TAKE_PROFIT_USD - 1e-9:
            return {"action": "SELL"}
        if pnl <= -STOP_LOSS_PER_SHARE_USD * shares + 1e-9:
            return {"action": "SELL"}
        if seconds <= FORCED_EXIT_SECONDS + 1e-9:
            return {"action": "SELL"}
        if seconds <= RECYCLE_EXIT_SECONDS + 1e-9 and pnl >= -RECYCLE_MAX_LOSS_USD - 1e-9:
            return {"action": "SELL"}
        return {"action": "HOLD"}

    def _enter(self, observation: dict[str, Any], account: dict[str, Any],
               books: dict[str, Any], now: float, seconds: float,
               gap: float | None) -> dict[str, Any]:
        if self.completed_cycles >= MAX_CYCLES_PER_MARKET:
            return {"action": "HOLD"}
        if now - self.last_flat_at < REENTRY_COOLDOWN_S - 1e-9:
            return {"action": "HOLD"}
        if now - self.last_buy_attempt_at < REENTRY_COOLDOWN_S - 1e-9:
            return {"action": "HOLD"}
        if not (ENTRY_MIN_SECONDS < seconds <= ENTRY_MAX_SECONDS):
            return {"action": "HOLD"}
        if gap is None or not self._gap_is_persistent(gap, now):
            return {"action": "HOLD"}

        side = "YES" if gap > 0 else "NO"
        other = "NO" if side == "YES" else "YES"
        side_book = books.get(side, {})
        asks = _levels(side_book.get("asks", []), reverse=False)
        if not asks or asks[0][0] > MAX_ENTRY_PRICE + 1e-9:
            return {"action": "HOLD"}

        # The book's own favourite must agree with the reference signal.
        def _mid(name: str) -> float | None:
            b = _levels(books.get(name, {}).get("bids", []), reverse=True)
            a = _levels(books.get(name, {}).get("asks", []), reverse=False)
            if b and a:
                return (b[0][0] + a[0][0]) / 2
            return None

        side_mid, other_mid = _mid(side), _mid(other)
        if side_mid is not None and other_mid is not None and side_mid < other_mid - 1e-9:
            return {"action": "HOLD"}

        ceiling = min(float(account["cashUsd"]),
                      float(observation["rules"]["maximumBuyCashUsd"]))
        min_size = float(side_book.get("minOrderSize", 0) or 0)
        exit_depth = _depth(side_book.get("bids", []), buying=False)

        # Largest size that fits the fee-inclusive ceiling, the ask
        # depth and the immediately executable exit depth.
        size = None
        candidate = math.floor(min(_depth(side_book.get("asks", []), buying=True),
                                   exit_depth) / SHARE_STEP) * SHARE_STEP
        while candidate >= max(min_size, SHARE_STEP) - 1e-9 and candidate > 0:
            cost = _walk(side_book.get("asks", []), candidate, buying=True)
            if cost is not None and cost[0] + cost[1] <= ceiling + 1e-9:
                size = candidate
                break
            candidate = round(candidate - SHARE_STEP, 6)
        if size is None:
            return {"action": "HOLD"}

        buy = _walk(side_book.get("asks", []), size, buying=True)
        sell = _walk(side_book.get("bids", []), size, buying=False)
        if buy is None or sell is None:
            return {"action": "HOLD"}
        gross, fees = buy
        sell_gross, sell_fees = sell
        roundtrip_loss = gross + fees - (sell_gross - sell_fees)
        if roundtrip_loss > MAX_IMMEDIATE_LOSS_PER_SHARE_USD * size + 1e-9:
            return {"action": "HOLD"}

        self.last_buy_attempt_at = now
        return {"action": "BUY", "outcome": side,
                "maxCashUsd": round(gross + fees, 6)}


BOT = VolumeBot()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            response = BOT.decide(json.loads(self.rfile.read(length)))
            body = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
