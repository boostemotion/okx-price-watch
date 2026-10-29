"""Mocked-price tests: hit / no-hit / dedupe / cooldown re-arm / half-to-stop / end date / test mode."""
import contextlib, datetime as dt, io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watch

BJT = watch.BJT
# Wed 2026-11-04 03:00 UTC+8 = Tue 14:00 New York -> US session (ORCL th 2.5)
T_US = dt.datetime(2026, 11, 4, 3, 0, tzinfo=BJT)
# Sat 2026-10-31 12:00 UTC+8 -> off session (ORCL th 3.5)
T_OFF = dt.datetime(2026, 10, 31, 12, 0, tzinfo=BJT)


def m(price, now, c1, c2):
    return {"price": price, "now": now, "c1": c1, "c2": c2}


def flat():
    return {i: m(100.0, 0.1, 0.1, 0.1) for i in watch.load_params()["instruments"].values()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        watch.STATE = os.path.join(self.tmp, "state.json")
        os.environ.pop("POSITIONS_JSON", None)
        os.environ["HERMES_WEBHOOK_URL"] = "https://example.invalid/hook"
        os.environ["HERMES_WEBHOOK_KEY"] = "k"
        self.P = watch.load_params()
        self.posted = []

    def post(self, b, cfg):
        self.posted.append(b); return True

    def go(self, metrics, t=T_US, mode="live", post=None):
        return watch.run(mode, P=self.P, t=t, fetch=lambda P, pos: (metrics, []),
                         post=post or self.post)


class Moves(Base):
    def test_no_hit(self):
        self.assertEqual(self.go(flat()), 0)
        self.assertEqual(self.posted, [])

    def test_hit_needs_both_closed_bars(self):
        mt = flat(); mt["ORCL-USDT-SWAP"] = m(105, 3.0, 2.6, 2.4)  # c2 below 2.5
        self.go(mt); self.assertEqual(self.posted, [])
        mt["ORCL-USDT-SWAP"] = m(105, 3.0, 2.6, 2.5)
        self.go(mt)
        self.assertEqual(len(self.posted), 1)
        b = self.posted[0]
        self.assertEqual((b["instId"], b["direction"], b["reason"], b["threshold_pct"]),
                         ("ORCL-USDT-SWAP", "up", "price_move", 2.5))

    def test_off_session_threshold(self):
        mt = flat(); mt["ORCL-USDT-SWAP"] = m(105, 3.0, 3.0, 3.0)
        self.go(mt, t=T_OFF); self.assertEqual(self.posted, [])   # off th = 3.5
        mt["NVDA-USDT-SWAP"] = m(90, -5.0, -4.6, -4.5)
        self.go(mt, t=T_OFF)
        self.assertEqual([(b["ticker"], b["direction"]) for b in self.posted], [("NVDA", "down")])

    def test_dedupe_cooldown_and_rearm(self):
        mt = flat(); mt["TSLA-USDT-SWAP"] = m(100.0, 4, 3.6, 3.6)
        self.go(mt); self.assertEqual(len(self.posted), 1)
        self.go(mt, t=T_US + dt.timedelta(minutes=5)); self.assertEqual(len(self.posted), 1)  # deduped
        mt["TSLA-USDT-SWAP"] = m(102.1, 4, 3.6, 3.6)   # +2.1% further > 2.0 -> re-alert in cooldown
        self.go(mt, t=T_US + dt.timedelta(minutes=10)); self.assertEqual(len(self.posted), 2)
        mt["TSLA-USDT-SWAP"] = m(102.1, 4, 3.6, 3.6)
        self.go(mt, t=T_US + dt.timedelta(minutes=71)); self.assertEqual(len(self.posted), 3)  # cooldown over

    def test_skuu_further_3pct(self):
        mt = flat(); mt["SKUU-USDT-SWAP"] = m(20.0, 5, 4.6, 4.6)
        self.go(mt)
        mt["SKUU-USDT-SWAP"] = m(20.5, 5, 4.6, 4.6)   # +2.5% < 3.0 -> suppressed
        self.go(mt, t=T_US + dt.timedelta(minutes=5)); self.assertEqual(len(self.posted), 1)

    def test_failed_post_not_deduped(self):
        mt = flat(); mt["TSLA-USDT-SWAP"] = m(100.0, 4, 3.6, 3.6)
        self.assertEqual(self.go(mt, post=lambda b, c: False), 2)
        self.go(mt); self.assertEqual(len(self.posted), 1)

    def test_dry_run_no_post_no_state(self):
        mt = flat(); mt["TSLA-USDT-SWAP"] = m(100.0, 4, 3.6, 3.6)
        self.go(mt, mode="dry")
        self.assertEqual(self.posted, []); self.assertFalse(os.path.exists(watch.STATE))

    def test_after_end_date(self):
        mt = flat(); mt["TSLA-USDT-SWAP"] = m(100.0, 4, 3.6, 3.6)
        t = dt.datetime(2026, 11, 23, 0, 0, 1, tzinfo=BJT)
        self.assertEqual(self.go(mt, t=t), 0); self.assertEqual(self.posted, [])
        self.go(mt, t=dt.datetime(2026, 11, 22, 23, 59, tzinfo=BJT))  # still inside window (Sun, off)
        self.assertEqual(self.posted, [])  # off th 5.0 > 3.6; window open but no hit
        mt["TSLA-USDT-SWAP"] = m(100.0, 6, 5.1, 5.1)
        self.go(mt, t=dt.datetime(2026, 11, 22, 23, 59, tzinfo=BJT)); self.assertEqual(len(self.posted), 1)


class HalfToStop(Base):
    def setpos(self, side, entry, sl):
        os.environ["POSITIONS_JSON"] = json.dumps({"positions": [
            {"instId": "NVDA-USDT-SWAP", "side": side, "size": 1, "entry": entry, "sl": sl}]})

    def test_long_half(self):
        self.setpos("long", 200, 190)
        mt = flat(); mt["NVDA-USDT-SWAP"] = m(196, 0, 0, 0)
        self.go(mt); self.assertEqual(self.posted, [])
        mt["NVDA-USDT-SWAP"] = m(195, 0, 0, 0)
        self.go(mt); self.assertEqual(self.posted[-1]["reason"], "half_to_stop")
        self.go(mt); self.assertEqual(len(self.posted), 1)            # once per entry/sl
        self.setpos("long", 200, 192)                                   # sl moved -> re-armed
        self.go(mt); self.assertEqual(len(self.posted), 2)

    def test_short_half(self):
        self.setpos("short", 200, 210)
        mt = flat(); mt["NVDA-USDT-SWAP"] = m(205.5, 0, 0, 0)
        self.go(mt); self.assertEqual(self.posted[0]["direction"], "up")
        self.assertEqual((self.posted[0]["entry"], self.posted[0]["sl"]), (200, 210))  # payload keeps it

    def test_unset_secret_skips_rule(self):
        mt = flat(); mt["NVDA-USDT-SWAP"] = m(150, 0, 0, 0)
        self.assertEqual(self.go(mt), 0); self.assertEqual(self.posted, [])

    def test_bad_json_skips_rule(self):
        os.environ["POSITIONS_JSON"] = "{not json"
        self.assertEqual(self.go(flat()), 0); self.assertEqual(self.posted, [])

    def test_public_outputs_have_no_position_data(self):
        self.setpos("long", 200.37, 190.11)
        os.environ["POSITIONS_JSON"] = json.dumps({"positions": [
            {"instId": "NVDA-USDT-SWAP", "side": "long", "size": 7, "entry": 200.37, "sl": 190.11},
            {"instId": "AAPL-USDT-SWAP", "side": "short", "size": 3, "entry": 250.55, "sl": 260.66}]})
        mt = flat(); mt["NVDA-USDT-SWAP"] = m(195.01, 0, 0, 0)
        mt["AAPL-USDT-SWAP"] = m(255.99, 0, 0, 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.go(mt)
            self.go(mt, mode="test")
        self.assertEqual(len(self.posted), 3)  # 2 half-to-stop + 1 test
        with open(watch.STATE) as f:
            state = f.read()
        public = out.getvalue() + state
        for bad in ("200.37", "190.11", "250.55", "260.66", "195.01", "255.99", "AAPL",
                    "long", "short", "size", "half_to_stop", "NVDA"):
            self.assertNotIn(bad, public, bad)
        self.assertIn("命中 2 条", out.getvalue())
        self.assertTrue(all(k.startswith("h:") for k in json.loads(state)["positions"]))


class TestMode(Base):
    def test_test_payload(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.go(flat(), mode="test")
        self.assertIn("读取价格 5/5", out.getvalue())
        b = self.posted[0]
        self.assertEqual(b["reason"], "test"); self.assertTrue(b["test"])
        self.assertIn("[TEST] 测试消息，不是真实报警，请勿下单", b["message"])
        self.assertEqual(len(b["prices"]), 5)
        self.assertFalse(os.path.exists(watch.STATE))


class Session(unittest.TestCase):
    def test_session(self):
        self.assertEqual(watch.session(T_US), "us")
        self.assertEqual(watch.session(T_OFF), "off")


if __name__ == "__main__":
    unittest.main(verbosity=2)
