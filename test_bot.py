#!/usr/bin/env python3
"""Behavioural self-tests for yeno_volume_bot.decide (not a score)."""
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from yeno_volume_bot import VolumeBot  # noqa: E402

SAMPLE = json.loads((Path(__file__).parent / "yeno-sample-observation.json").read_text())


def obs(**over):
    o = copy.deepcopy(SAMPLE)
    for key, value in over.items():
        if key in ("market", "account", "reference", "rules"):
            o[key].update(value)
        else:
            o[key] = value
    return o


def run():
    failures = []

    def check(name, got, want_action):
        ok = got.get("action") == want_action
        print(f"{'PASS' if ok else 'FAIL'}: {name} -> {got}")
        if not ok:
            failures.append(name)

    # 1. Persistent qualifying gap -> BUY YES on the second look, near-full budget.
    bot = VolumeBot()
    t0 = SAMPLE["timestamp"]
    check("first look (no persistence yet)", bot.decide(obs()), "HOLD")
    o2 = obs(timestamp=t0 + 1.0)
    o2["reference"]["observedAt"] = t0 + 0.9
    start = time.perf_counter()
    got = bot.decide(o2)
    elapsed_ms = (time.perf_counter() - start) * 1000
    check("persistent gap", got, "BUY")
    if got.get("action") == "BUY":
        assert got["outcome"] == "YES", got
        assert 4.5 < got["maxCashUsd"] <= 5.0, got
        print(f"  size check: maxCashUsd={got['maxCashUsd']} (house bot spent ~2.60)")
    print(f"  decide latency: {elapsed_ms:.2f} ms (limit 200 ms)")
    assert elapsed_ms < 200

    # 2. Stale reference -> HOLD.
    bot = VolumeBot()
    bot.decide(obs())
    stale = obs(timestamp=t0 + 1.0)
    stale["reference"]["observedAt"] = t0 - 30.0
    check("stale reference", bot.decide(stale), "HOLD")

    # 3. Provisional target -> HOLD.
    bot = VolumeBot()
    bot.decide(obs())
    prov = obs(timestamp=t0 + 1.0)
    prov["reference"]["targetProvisional"] = True
    prov["reference"]["observedAt"] = t0 + 0.9
    check("provisional target", bot.decide(prov), "HOLD")

    # 4. Small gap -> HOLD even when persistent.
    bot = VolumeBot()
    small = obs()
    small["reference"]["btcMidUsd"] = small["reference"]["openingTargetUsd"] + 10.0
    bot.decide(small)
    small2 = obs(timestamp=t0 + 1.0)
    small2["reference"]["btcMidUsd"] = small2["reference"]["openingTargetUsd"] + 10.0
    small2["reference"]["observedAt"] = t0 + 0.9
    check("small gap", bot.decide(small2), "HOLD")

    # 5. Negative gap -> BUY NO.
    bot = VolumeBot()
    neg = obs()
    neg["reference"]["btcMidUsd"] = neg["reference"]["openingTargetUsd"] - 80.0
    bot.decide(neg)
    neg2 = obs(timestamp=t0 + 1.0)
    neg2["reference"]["btcMidUsd"] = neg2["reference"]["openingTargetUsd"] - 80.0
    neg2["reference"]["observedAt"] = t0 + 0.9
    got = bot.decide(neg2)
    check("negative gap side", got, "BUY")
    if got.get("action") == "BUY":
        assert got["outcome"] == "NO", got

    # 6. Open position, profitable exit available -> SELL.
    bot = VolumeBot()
    pos = obs(account={"position": {
        "market_id": SAMPLE["market"]["id"], "outcome": "YES", "shares": 9.5,
        "buy_gross_usd": 4.75, "buy_fees_usd": 0.21,
        "sell_gross_usd": 0, "sell_fees_usd": 0}})
    pos["books"]["YES"]["bids"] = [[0.56, 100]]
    check("take profit", bot.decide(pos), "SELL")

    # 7. Open position, small unrealised loss, plenty of time -> HOLD.
    bot = VolumeBot()
    pos = obs(account={"position": {
        "market_id": SAMPLE["market"]["id"], "outcome": "YES", "shares": 9.5,
        "buy_gross_usd": 4.75, "buy_fees_usd": 0.21,
        "sell_gross_usd": 0, "sell_fees_usd": 0}})
    pos["books"]["YES"]["bids"] = [[0.495, 100]]
    pos["market"]["secondsToClose"] = 200
    check("small loss, time remains", bot.decide(pos), "HOLD")

    # 8. Open position near the close -> SELL (forced flat).
    bot = VolumeBot()
    pos = obs(account={"position": {
        "market_id": SAMPLE["market"]["id"], "outcome": "YES", "shares": 9.5,
        "buy_gross_usd": 4.75, "buy_fees_usd": 0.21,
        "sell_gross_usd": 0, "sell_fees_usd": 0}})
    pos["books"]["YES"]["bids"] = [[0.495, 100]]
    pos["market"]["secondsToClose"] = 40
    check("forced exit near close", bot.decide(pos), "SELL")

    # 9. Too early in the market -> HOLD.
    bot = VolumeBot()
    early = obs()
    early["market"]["secondsToClose"] = 280
    bot.decide(early)
    early2 = obs(timestamp=t0 + 1.0)
    early2["market"]["secondsToClose"] = 279
    early2["reference"]["observedAt"] = t0 + 0.9
    check("too early to enter", bot.decide(early2), "HOLD")

    # 10. Final-seconds entry blocked.
    bot = VolumeBot()
    late = obs()
    late["market"]["secondsToClose"] = 18
    bot.decide(late)
    late2 = obs(timestamp=t0 + 1.0)
    late2["market"]["secondsToClose"] = 17
    late2["reference"]["observedAt"] = t0 + 0.9
    check("final seconds entry", bot.decide(late2), "HOLD")

    print("FAILURES:", failures if failures else "none")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    run()
