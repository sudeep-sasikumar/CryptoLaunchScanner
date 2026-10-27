"""Decoding, token state, labels, factors, vetoes and reputation."""
from __future__ import annotations

from radar.db import DB
from radar.features import pump_features, social_key, trade_features
from radar.scoring import gate, prior_score
from radar.sources.pump_events import decode_logs, decode_trade
from radar.state import Label, PumpToken
from radar.wallets import Reputation

from .helpers import SYSTEM, Curve, addr, cfg, create_bytes, log_line, trade_bytes

C = cfg()


# ---------------------------------------------------------------- event decoding
def test_decode_create_and_trade_from_logs():
    logs = ["Program log: Instruction: Create", log_line(create_bytes(1, 100, name="Doge 2", symbol="D2")),
            log_line(trade_bytes(1, 100, True, 500_000_000, 17_000_000_000_000, 30_500_000_000, 1_056_000_000_000_000, 1_000_000)),
            "Program data: not-base64!!", "Program data: AAAA"]
    events = decode_logs(logs)
    assert [k for k, _ in events] == ["create", "trade"]
    create, trade = events[0][1], events[1][1]
    assert create["name"] == "Doge 2" and create["symbol"] == "D2" and create["mint"] == addr(1)
    assert create["user"] == addr(100) and create["quote_mint"] == SYSTEM
    assert trade["is_buy"] is True and trade["user"] == addr(100)
    assert trade["quote_amount"] == 500_000_000 and trade["ix_name"] == "buy"


def test_decode_trade_survives_shorter_and_longer_layouts():
    full = trade_bytes(1, 2, False, 10, 20, 30, 40, 99)
    old = decode_trade(trade_bytes(1, 2, False, 10, 20, 30, 40, 99, tail=False))     # an older, shorter event
    assert old["sol_amount"] == 10 and old["token_amount"] == 20 and "quote_mint" not in old
    future = decode_trade(full + b"\x01" * 50)                                        # fields added later
    assert future["virtual_quote_reserves"] == 30 and future["is_buy"] is False
    assert decode_trade(full[:60]) is None                                            # too short to mean anything


# ---------------------------------------------------------------- labels
def _label(price=1.0):
    return Label(1, 1000.0, price, C.label)


def test_label_hit_needs_target_above_entry_with_slippage():
    lb = _label()
    assert lb.update(1010, 2.0) is None            # 2.0 / 1.03 = 1.94x: not there yet
    assert lb.update(1020, 2.1) == "hit" and lb.hit and not lb.done
    assert lb.update(1030, 3.0) is None and round(lb.peak, 2) == round(3.0 / 1.03, 2)
    lb.update(1040, 0.4)                           # gave it all back: tracking ends, still a hit
    assert lb.done and lb.hit and lb.end_reason == "faded"


def test_label_stop_before_target_is_a_loss_even_if_it_runs_later():
    lb = _label()
    assert lb.update(1010, 0.5) == "stop" and lb.done and not lb.hit
    assert lb.update(1020, 5.0) is None and not lb.hit


def test_label_horizon_and_prices_before_snapshot_ignored():
    lb = _label()
    assert lb.update(999, 10.0) is None and lb.peak < 1.0
    lb.tick(1000 + C.label.horizon_s + 1)
    assert lb.done and lb.end_reason == "horizon" and not lb.hit


# ---------------------------------------------------------------- token state
def _token(curve: Curve) -> PumpToken:
    return PumpToken(curve.create_event(), curve.slot, 9, C.pump)


def _clean_launch(n_buyers=40):
    """Dev buys a little; buyers arrive one per second, each in its own slot."""
    c = Curve()
    tok = _token(c)
    tok.on_trade(*c.buy(100, 0.5))
    for i in range(n_buyers):
        tok.on_trade(*c.buy(200 + i, 0.3 + 0.01 * i, dt=3 + i, dslot=10 + i * 2))
    return c, tok


def _bundled_launch():
    """Five wallets take a big share in the creation slot, snipers follow, the dev dumps."""
    c = Curve()
    tok = _token(c)
    tok.on_trade(*c.buy(100, 2.0))
    for i in range(5):
        tok.on_trade(*c.buy(300 + i, 2.0, dt=0, dslot=0))
    for i in range(4):
        tok.on_trade(*c.buy(400 + i, 1.0, dt=1, dslot=2))
    for i in range(12):
        tok.on_trade(*c.buy(500 + i, 0.2, dt=5 + i, dslot=20 + i))
    tok.on_trade(*c.sell(100, tok.wallets[addr(100)][0], dt=20, dslot=60))
    return c, tok


def test_clean_launch_factors():
    c, tok = _clean_launch()
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": 100.0, "quote_kind": 0})
    assert f["launch_slot0_n"] == 0 and f["launch_bundle_pct"] == 0
    assert f["dist_holders"] == 40 and f["flow_buys"] == 41 and f["flow_sells"] == 0
    assert 0 < f["launch_dev_hold_pct"] < 3 and f["launch_dev_sold_pct"] == 0
    assert f["px_mult"] > 1.5 and f["px_dd_pct"] == 0
    assert abs(f["px_mcap_usd"] - tok.price * 1e9 * 100) < 1e-6
    assert f["launch_phantom_sold_pct"] == 0 and f["launch_same_size_share"] == 0


def test_bundled_launch_is_detected_and_vetoed():
    c, tok = _bundled_launch()
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": 100.0, "quote_kind": 0})
    assert f["launch_slot0_n"] == 5 and f["launch_sniper_n"] == 4
    assert f["launch_slot0_pct"] > 15 and f["launch_bundle_pct"] > f["launch_slot0_pct"]
    assert f["launch_dev_sold_pct"] == 100 and f["launch_dev_hold_pct"] == 0
    assert f["launch_same_size_share"] > 0.3                     # twelve identical 0.2 SOL buys
    assert f["launch_bundle_held_pct"] > 25 and "still hold" in gate(f, C)
    assert gate(f, C, learned=True) is None             # an opinion veto: a trained model judges it instead
    assert "rugged" in gate({**f, "rc_rugged": 1}, C, learned=True)      # a safety veto: always applies
    clean = pump_features(_clean_launch()[1], 60, c.ts + 60, {"quote_usd": 100.0, "quote_kind": 0})
    assert gate(clean, C) is None
    assert prior_score(clean)[0] > prior_score(f)[0] + 10


def test_sell_by_wallet_that_never_bought_counts_as_phantom():
    c, tok = _clean_launch(10)
    tok.on_trade(*c.sell(999, 5_000_000, dt=30, dslot=90))       # tokens arrived by transfer, not purchase
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": 100.0})
    assert abs(f["launch_phantom_sold_pct"] - 0.5) < 1e-6


def test_bars_feed_labels_and_final_stats_classify_the_ending():
    c, tok = _clean_launch(20)
    lb = Label(1, c.ts + 30, tok.price, C.label)
    tok.labels.append(lb)
    events = []
    for i in range(40):                                          # a strong run ...
        events += tok.on_trade(*c.buy(600 + i, 1.5, dt=40 + i * 5, dslot=200 + i))
    assert any(ev == "hit" for _, ev in events) and lb.hit
    holders = [(w, r[0]) for w, r in tok.wallets.items() if r[0] > 0]
    for j, (w, bal) in enumerate(holders):                       # ... then everyone leaves
        e, slot = c.sell(0, bal, dt=300 + j, dslot=900 + j)
        e["user"] = w
        tok.on_trade(e, slot)
    tok.flush(c.ts + 2000)
    final = tok.final_stats(c.ts + 2000)
    assert final["peak_mult"] > 3 and final["kind"] in ("fast_fade", "dev_dump", "whale_dump")
    assert final["path"] and final["path"][0][1] > 1


def test_unknown_quote_price_blocks_a_call_but_not_the_snapshot():
    c, tok = _clean_launch()
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": None, "quote_kind": 3})
    assert f["px_mcap_usd"] is None and f["px_mult"] > 1
    assert "quote" in gate(f, C)


# ---------------------------------------------------------------- other pieces
def test_social_key_tells_an_account_from_a_post():
    assert social_key("https://x.com/SomeCoin") == ("somecoin", 1)
    assert social_key("https://twitter.com/elonmusk/status/123?s=20") == ("elonmusk", 2)
    assert social_key("https://x.com/i/communities/1234567")[1] == 3
    assert social_key("https://x.com/search?q=dog")[1] == 4
    assert social_key("https://t.me/mygroup/") == ("mygroup", 1)
    assert social_key("") == ("", 0)


def test_seen_counter_and_burst_window(tmp_path):
    db = DB(tmp_path / "t.db")
    assert db.seen_bump("name", "doge", 1000) == (0, 1000, 0)
    assert db.seen_bump("name", "doge", 1100) == (1, 1000, 1)
    assert db.seen_bump("name", "doge", 5000)[::2] == (2, 0)      # 2 before, none inside the 10-minute window
    assert db.seen_count("name", "doge") == 3 and db.seen_count("name", "cat") == 0


def test_trade_features_spot_a_first_block_bundle():
    def t(block, wallet, kind, usd):
        return {"attributes": {"block_number": block, "tx_from_address": wallet, "kind": kind, "volume_in_usd": str(usd),
                               "price_to_in_usd": "0.001", "price_from_in_usd": "0.001",
                               "block_timestamp": "2026-10-13T23:34:21Z"}}
    trades = [t(10, f"b{i}", "buy", 500) for i in range(4)] + [t(20 + i, f"w{i}", "buy", 50) for i in range(6)]
    f = trade_features(trades, "tok")
    assert f["launch_block0_n"] == 4 and round(f["launch_block0_pct"]) == 87
    assert f["dist_buyers"] == 10 and f["launch_same_size_share"] == 0.6 and len(f["entries"]) == 10


def test_reputation_marks_repeat_winners_smart_and_bundlers_toxic(tmp_path):
    db = DB(tmp_path / "t.db")
    rep = Reputation(db, C.learn.wallets)
    rep.base = {"n": 10000, "win": 1000, "dead": 6000}
    rows = [("smart", 10, 7, 1, 0, 0, 50.0), ("lucky_once", 1, 1, 0, 0, 0, 5.0), ("bundler", 12, 0, 12, 12, 0, 3.0),
            ("dumper", 9, 2, 3, 0, 4, 9.0), ("loser", 10, 6, 2, 0, 0, -20.0)]
    db.executemany("INSERT INTO wallets(addr,n_tok,n_win,n_dead,n_slot0,n_dump,pnl,last_ts) VALUES(?,?,?,?,?,?,?,0)", rows)
    db.commit()
    s = rep.summarize(["smart", "lucky_once", "bundler", "dumper", "loser", "never_seen"])
    assert s["wq_smart_n"] == 1 and s["_smart"] == ["smart"]     # one win proves nothing; winners who lose money are not smart
    assert s["wq_toxic_n"] == 2 and s["wq_bot_n"] == 1
    assert abs(s["wq_known_share"] - 5 / 6) < 1e-9


def test_credit_updates_wallets_and_creator(tmp_path):
    db = DB(tmp_path / "t.db")
    rep = Reputation(db, C.learn.wallets)
    c, tok = _clean_launch(20)
    rep.creator_launched(tok.user, c.ts)
    for i in range(40):
        tok.on_trade(*c.buy(600 + i, 1.5, dt=40 + i * 5, dslot=200 + i))
    tok.flush(c.ts + 5000)
    rep.credit(tok, tok.final_stats(c.ts + 5000), 100.0)
    db.commit()
    first = db.one("SELECT * FROM wallets WHERE addr=?", (addr(200),))
    assert first["n_tok"] == 1 and first["n_win"] == 1 and first["n_slot0"] == 0
    assert db.one("SELECT COUNT(*) n FROM wallets")["n"] == 20 + 20       # first 40 distinct non-dev buyers
    cr = db.one("SELECT * FROM creators WHERE addr=?", (tok.user,))
    assert cr["n_created"] == 1 and cr["n_snap"] == 1 and cr["n_win"] == 1
    assert rep.base["n"] == 40 and rep.base["win"] >= 20


def test_label_records_how_tradeable_the_move_was():
    import json
    from radar.state import Label
    from .helpers import cfg
    lb = Label(1, 0.0, 1.0 / 1.03, cfg().label)
    for t, p in ((15, 0.9), (30, 1.2), (60, 1.6), (90, 2.1), (150, 2.4), (200, 1.7), (260, 3.2)):
        lb.update(t, p)
    tx = json.loads(lb.tradeable(300))
    assert tx["t"] == {"1.5": 60, "2": 90, "3": 260} and tx["hold2"] == 110      # 2x at 90 s, below 1.8x at 200 s
    assert tx["low_pre"] == 0.9 and tx["m30"] == 1.2 and tx["m60"] == 1.6
