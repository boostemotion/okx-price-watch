"""Mocked-OKX tests for fill_watch: seed, dedupe, instId filter, restart, retry, end date, clean logs."""
import contextlib, datetime as dt, io, json, os, subprocess, sys, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import fill_watch as fw

BJT = fw.BJT
T0 = dt.datetime(2026, 11, 2, 10, 0, tzinfo=BJT)
SECRET_BITS = ("RO-API-KEY-123", "RO-SECRET-xyz", "RO-PASS", "example.invalid", "hookkey-999")
_tid = [1000]


def fill(inst, at, px="12.34", sz="7", side="buy"):
    _tid[0] += 1
    ms = int(at.timestamp() * 1000)
    return {"instType": "SWAP", "instId": inst, "tradeId": str(_tid[0]), "billId": str(9_000_000 + _tid[0]),
            "ordId": "77" + str(_tid[0]), "clOrdId": "", "side": side, "posSide": "net", "subType": "1",
            "execType": "T", "fillPx": px, "fillSz": sz, "ts": str(ms), "fillTime": str(ms - 5)}


class FakeOKX:
    """Exchange account with fills on our swaps and on Elian's manual ones (AAPL, KO)."""
    def __init__(self):
        self.fills, self.calls = [], []

    def get(self, path, params, creds):
        self.calls.append((path, dict(params)))
        rows = [f for f in self.fills if (not params.get("instId") or f["instId"] == params["instId"])]
        rows.sort(key=lambda f: -int(f["billId"]))
        if "after" in params:
            rows = [f for f in rows if int(f["billId"]) < int(params["after"])]
        return rows[: int(params.get("limit", 100))]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fw.STATE = os.path.join(self.tmp, "state", "fill-state.json")
        os.environ.update(OKX_RO_API_KEY=SECRET_BITS[0], OKX_RO_SECRET_KEY=SECRET_BITS[1],
                          OKX_RO_PASSPHRASE=SECRET_BITS[2],
                          HERMES_WEBHOOK_URL="https://example.invalid/hook", HERMES_WEBHOOK_KEY=SECRET_BITS[4])
        os.environ.pop("FILL_STATE_SALT", None)
        self.okx, self.posted, self.fail_post = FakeOKX(), [], False

    def post(self, body, cfg):
        if self.fail_post:
            return False
        self.posted.append(body)
        return True

    def go(self, now, mode="live"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fw.run(mode, now=now, get=self.okx.get, post=self.post)
        self.last_log = out.getvalue()
        return rc


class FillWatch(Base):
    def test_first_run_seeds_without_alert(self):
        self.okx.fills += [fill("TSLA-USDT-SWAP", T0 - dt.timedelta(minutes=30)), fill("AAPL-USDT-SWAP", T0)]
        self.assertEqual(self.go(T0), 0)
        self.assertEqual(self.posted, [])
        self.assertIn("已建立基线", self.last_log)
        self.assertTrue(os.path.exists(fw.STATE))

    def test_new_fill_alerts_once_and_only_our_5(self):
        self.go(T0)
        t1 = T0 + dt.timedelta(minutes=2)
        self.okx.fills += [fill("NVDA-USDT-SWAP", t1, side="sell"), fill("AAPL-USDT-SWAP", t1),
                           fill("KO-USDT-SWAP", t1), fill("ORCL-USDT-SWAP", t1)]
        self.assertEqual(self.go(t1 + dt.timedelta(seconds=30)), 0)
        self.assertEqual(sorted(b["instId"] for b in self.posted), ["NVDA-USDT-SWAP", "ORCL-USDT-SWAP"])
        self.assertIn("检查完成，新成交 2 条，已发送 2 条", self.last_log)
        b = [x for x in self.posted if x["instId"] == "NVDA-USDT-SWAP"][0]
        self.assertEqual((b["source"], b["side"], b["fillPx"], b["fillSz"]), ("fill-watch", "sell", "12.34", "7"))
        self.assertTrue(b["ts"].endswith("+08:00"))
        for k in ("bal", "balance", "availBal", "eq"):
            self.assertNotIn(k, b)
        # never asked OKX for anything but our 5
        self.assertTrue(all(p["instId"] in fw.INSTS for _, p in self.okx.calls))
        # same process, rerun: no repeat
        self.go(t1 + dt.timedelta(minutes=2))
        self.assertEqual(len(self.posted), 2)
        self.assertIn("新成交 0 条", self.last_log)

    def test_restart_fresh_process_does_not_repeat(self):
        self.go(T0)
        t1 = T0 + dt.timedelta(minutes=50)
        self.okx.fills.append(fill("SKUU-USDT-SWAP", t1))
        self.go(t1 + dt.timedelta(minutes=1))
        self.assertEqual(len(self.posted), 1)
        with open(os.path.join(self.tmp, "fills.json"), "w") as f:
            json.dump(self.okx.fills, f)
        # fresh interpreter, same state file, crossing an hour boundary
        code = f"""
import json, sys, datetime as dt; sys.path.insert(0, {HERE!r}); import fill_watch as fw
fw.STATE = {fw.STATE!r}
fills = json.load(open({os.path.join(self.tmp, 'fills.json')!r}))
def get(path, params, creds): return [f for f in fills if f['instId'] == params['instId']]
posted = []
fw.run('live', now=dt.datetime(2026, 11, 2, 11, 5, tzinfo=fw.BJT), get=get, post=lambda b, c: posted.append(b) or True)
print('POSTED', len(posted))
"""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ)
        self.assertIn("POSTED 0", r.stdout, r.stderr)

    def test_failed_post_is_retried_next_run(self):
        self.go(T0)
        t1 = T0 + dt.timedelta(minutes=10)
        self.okx.fills.append(fill("SNDK-USDT-SWAP", t1))
        self.fail_post = True
        self.assertEqual(self.go(t1 + dt.timedelta(minutes=1)), 2)
        self.assertIn("已发送 0 条", self.last_log)
        self.fail_post = False
        # webhook down for 2 hours: mark must not have advanced, fill still alerted
        self.go(t1 + dt.timedelta(hours=2))
        self.assertEqual([b["instId"] for b in self.posted], ["SNDK-USDT-SWAP"])
        self.go(t1 + dt.timedelta(hours=2, minutes=2))
        self.assertEqual(len(self.posted), 1)

    def test_old_fills_and_gap_use_history(self):
        self.go(T0)
        t1 = T0 + dt.timedelta(days=4)
        self.okx.fills.append(fill("TSLA-USDT-SWAP", T0 + dt.timedelta(days=1)))
        self.go(t1)   # runs were down 4 days -> fills-history
        self.assertTrue(any(p.endswith("fills-history") for p, _ in self.okx.calls))
        self.assertEqual(len(self.posted), 1)

    def test_pagination_beyond_100(self):
        self.go(T0)
        t1 = T0 + dt.timedelta(minutes=5)
        self.okx.fills += [fill("ORCL-USDT-SWAP", t1, sz="1") for _ in range(150)]
        self.go(t1 + dt.timedelta(minutes=1))
        self.assertEqual(len(self.posted), 150)

    def test_end_date(self):
        self.go(T0)
        late = dt.datetime(2026, 11, 23, 0, 0, 1, tzinfo=BJT)
        self.okx.fills.append(fill("NVDA-USDT-SWAP", late))
        self.go(late)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.okx.calls[-1][0], "/api/v5/trade/fills")  # only from the seed run
        self.assertIn("不再报警", self.last_log)

    def test_missing_keys_exits_clean(self):
        for k in ("OKX_RO_API_KEY", "OKX_RO_SECRET_KEY", "OKX_RO_PASSPHRASE"):
            os.environ.pop(k)
        self.assertEqual(self.go(T0), 0)
        self.assertEqual(self.last_log.strip().splitlines()[-1][-10:], "未配置只读 key，跳过"[-10:])
        self.assertEqual(len(self.last_log.strip().splitlines()), 1)
        self.assertEqual(self.okx.calls, [])

    def test_test_mode(self):
        self.assertEqual(self.go(T0, mode="test"), 0)
        self.assertEqual(len(self.posted), 1)
        b = self.posted[0]
        self.assertTrue(b["test"])
        self.assertEqual(b["message"], "[TEST] 成交检测测试消息，不是真实成交，请勿操作")
        self.assertIn("OKX 只读接口 5/5 正常", self.last_log)
        self.assertFalse(os.path.exists(fw.STATE))

    def test_state_and_logs_reveal_nothing(self):
        logs = ""
        self.go(T0); logs += self.last_log
        t1 = T0 + dt.timedelta(minutes=3)
        f = fill("TSLA-USDT-SWAP", t1, px="345.67", sz="13")
        self.okx.fills.append(f)
        self.go(t1 + dt.timedelta(minutes=1)); logs += self.last_log
        with open(fw.STATE) as fh:
            st = fh.read()
        for s in (f["tradeId"], f["billId"], f["ordId"], "345.67", "TSLA", f["ts"]) + SECRET_BITS:
            self.assertNotIn(s, st)
            self.assertNotIn(s, logs)
        data = json.loads(st)
        self.assertEqual(set(data), {"v", "kid", "mark_hour_ms", "seen"})
        self.assertEqual(data["mark_hour_ms"] % 3_600_000, 0)
        for line in logs.strip().splitlines():
            self.assertRegex(line, r"UTC\+8 (检查完成，新成交 \d+ 条，已发送 \d+ 条|webhook HTTP \d+|首次运行/密钥更换：已建立基线，不报警)$")

    def test_crash_logs_type_only(self):
        r = subprocess.run([sys.executable, "-c", f"""
import sys; sys.path.insert(0, {HERE!r}); import fill_watch as fw
def boom(*a, **k): raise ValueError('detail RO-SECRET-xyz')
fw.run = boom; sys.argv = ['x']; fw.main()"""], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("运行出错: ValueError", r.stdout)
        self.assertNotIn("RO-SECRET", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
