#!/usr/bin/env python3
"""Read-only OKX fill detector: wakes Hermes when one of OUR 5 swaps gets a fill.

Uses a READ-ONLY OKX API key (GET /api/v5/trade/fills, /fills-history only; never trades).
Only SKUU/SNDK/TSLA/NVDA/ORCL-USDT-SWAP are queried; every other instrument is ignored.
The repo is PUBLIC: logs print counts only; state/fill-state.json holds keyed hashes + an hour mark.

  python fill_watch.py           one check: alert new fills, update state
  python fill_watch.py --test    one [TEST] webhook (plus an OKX auth check if keys exist), no state

Env (secrets): OKX_RO_API_KEY, OKX_RO_SECRET_KEY, OKX_RO_PASSPHRASE, HERMES_WEBHOOK_URL,
HERMES_WEBHOOK_KEY; optional HERMES_WEBHOOK_KEY_HEADERS, FILL_STATE_SALT, OKX_BASES.

Dedupe: a fill alerts iff instId is one of ours, its record ts >= floor (= hour mark - 1h) and
HMAC(salt, instId|tradeId|billId) is not in `seen`. The hour mark only advances after a run in
which every POST succeeded, so a failed POST is retried next run. No state file = first run:
seed every current fill as seen, alert nothing.
"""
import argparse, base64, datetime as dt, hashlib, hmac, json, os, sys, time
import urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

BJT = ZoneInfo("Asia/Shanghai")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state", "fill-state.json")
INSTS = ("SKUU-USDT-SWAP", "SNDK-USDT-SWAP", "TSLA-USDT-SWAP", "NVDA-USDT-SWAP", "ORCL-USDT-SWAP")
END = dt.datetime(2026, 11, 22, 23, 59, 59, tzinfo=BJT)
UA = "Mozilla/5.0"
HOUR_MS = 3_600_000
FILLS_WINDOW_MS = 3 * 24 * HOUR_MS          # /trade/fills covers the last 3 days
MAX_PAGES = 10                              # x100 fills per instrument per run
TEST_TEXT = "[TEST] 成交检测测试消息，不是真实成交，请勿操作"


def log(msg):
    print(f"{dt.datetime.now(BJT):%Y-%m-%d %H:%M:%S} UTC+8 {msg}", flush=True)


class OKXError(Exception):
    pass


# ---------- OKX private GET (read-only) ----------
def okx_creds():
    k, s, p = (os.environ.get(x, "") for x in ("OKX_RO_API_KEY", "OKX_RO_SECRET_KEY", "OKX_RO_PASSPHRASE"))
    return (k, s, p) if k and s and p else None


def okx_get(path, params, creds):
    key, secret, passphrase = creds
    qpath = path + "?" + urllib.parse.urlencode(params)
    bases = [b.strip() for b in os.environ.get("OKX_BASES", "www.okx.com").split(",") if b.strip()]
    last = "no base"
    for b in bases:
        now = dt.datetime.now(dt.timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        sign = base64.b64encode(hmac.new(secret.encode(), f"{ts}GET{qpath}".encode(),
                                         hashlib.sha256).digest()).decode()
        req = urllib.request.Request(f"https://{b}{qpath}", headers={
            "User-Agent": UA, "Accept": "application/json", "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": passphrase})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"          # body may echo request details; not logged
            continue
        except Exception as e:
            last = type(e).__name__
            continue
        if j.get("code") != "0":
            raise OKXError(f"okx code {j.get('code')}")  # code only; msg can carry ids
        return j["data"]
    raise OKXError(last)


def fetch_fills(creds, floor_ms, now_ms, get=okx_get):
    """All fills of our 5 instIds with record ts >= floor_ms (paginating by billId)."""
    path = "/api/v5/trade/fills" if floor_ms >= now_ms - FILLS_WINDOW_MS + HOUR_MS \
        else "/api/v5/trade/fills-history"
    out = []
    for inst in INSTS:
        after = None
        for _ in range(MAX_PAGES):
            params = {"instType": "SWAP", "instId": inst, "limit": "100"}
            if after:
                params["after"] = after
            page = get(path, params, creds)
            out += [f for f in page if f.get("instId") == inst]   # belt and braces
            if len(page) < 100 or min(int(f["ts"]) for f in page) < floor_ms:
                break
            after = page[-1]["billId"]
            time.sleep(0.25 if path.endswith("history") else 0.05)  # 10 req/2s on history
    return [f for f in out if int(f["ts"]) >= floor_ms and f.get("instId") in INSTS]


# ---------- state (public file: nothing readable) ----------
def salt_key():
    s = os.environ.get("FILL_STATE_SALT") or os.environ.get("OKX_RO_API_KEY", "")
    return hashlib.sha256(("fill-watch|" + s).encode()).digest()


def fill_hash(f, key):
    raw = f"{f['instId']}|{f['tradeId']}|{f.get('billId', '')}"
    return hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()[:16]


def key_id(key):
    return hashlib.sha256(b"kid|" + key).hexdigest()[:8]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    s["seen"] = sorted(set(s["seen"]))
    with open(STATE + ".tmp", "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(STATE + ".tmp", STATE)


# ---------- webhook (same header logic as watch.py) ----------
def webhook_cfg():
    g = lambda k, d=None: os.environ.get(k) or d
    return {"url": g("HERMES_WEBHOOK_URL"), "key": g("HERMES_WEBHOOK_KEY"),
            "headers": g("HERMES_WEBHOOK_KEY_HEADERS", "Authorization,X-Automation-Key")}


def post_alert(body, cfg):
    if not cfg["url"] or not cfg["key"]:
        log("ERROR webhook 未配置，未发送")
        return False
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    for h in [x.strip() for x in cfg["headers"].split(",") if x.strip()]:
        headers[h] = f"Bearer {cfg['key']}" if h.lower() == "authorization" else cfg["key"]
    req = urllib.request.Request(cfg["url"], data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        log(f"webhook 发送失败: {type(e).__name__}")
        return False
    log(f"webhook HTTP {code}")
    return 200 <= code < 300


def iso_bjt(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, BJT).isoformat(timespec="milliseconds")


def body(f):
    return {"source": "fill-watch", "reason": "fill", "instId": f["instId"], "side": f.get("side"),
            "posSide": f.get("posSide"), "subType": f.get("subType"), "execType": f.get("execType"),
            "fillPx": f.get("fillPx"), "fillSz": f.get("fillSz"), "ordId": f.get("ordId"),
            "clOrdId": f.get("clOrdId"), "tradeId": f.get("tradeId"),
            "ts": iso_bjt(f.get("fillTime") or f["ts"])}


# ---------- one run ----------
def run(mode, now=None, get=okx_get, post=post_alert):
    now = now or dt.datetime.now(BJT)
    now_ms = int(now.timestamp() * 1000)
    if mode == "test":
        creds = okx_creds()
        if creds:
            n = 0
            for inst in INSTS:
                try:
                    get("/api/v5/trade/fills", {"instType": "SWAP", "instId": inst, "limit": "1"}, creds)
                    n += 1
                except Exception as e:
                    log(f"OKX 鉴权/读取失败: {type(e).__name__} {e if isinstance(e, OKXError) else ''}")
                    break
            log(f"测试模式：OKX 只读接口 {n}/{len(INSTS)} 正常")
        else:
            log("测试模式：未配置 OKX 只读密钥，跳过 OKX 检查")
        ok = post({"source": "fill-watch", "reason": "test", "test": True, "message": TEST_TEXT,
                   "ts": now.isoformat(timespec="seconds")}, webhook_cfg())
        log("测试 webhook " + ("OK" if ok else "FAILED"))
        return 0 if ok else 1
    if now > END:
        log("已过 2026-11-22 23:59:59 UTC+8，不再报警")
        return 0
    creds = okx_creds()
    if not creds:
        log("未配置只读 key，跳过")
        return 0

    key = salt_key()
    kid = key_id(key)
    hour_now = now_ms // HOUR_MS * HOUR_MS
    state = load_state()
    seed = state is None or state.get("kid") != kid
    floor = (hour_now if seed else int(state["mark_hour_ms"])) - HOUR_MS
    hashes = {fill_hash(f, key): f for f in fetch_fills(creds, floor, now_ms, get)}

    if seed:
        save_state({"v": 1, "kid": kid, "mark_hour_ms": hour_now, "seen": list(hashes)})
        log("首次运行/密钥更换：已建立基线，不报警")
        log("检查完成，新成交 0 条，已发送 0 条")
        return 0

    seen_before = set(state["seen"])
    new = [(h, f) for h, f in hashes.items() if h not in seen_before]
    new.sort(key=lambda hf: int(hf[1]["ts"]))
    cfg, sent, failed = webhook_cfg(), set(), False
    for h, f in new:
        if post(body(f), cfg):
            sent.add(h)
        else:
            failed = True
    # keep only hashes still inside the window we fetch next time (+ everything just sent)
    keep = {h for h in hashes if h in seen_before or h in sent}
    save_state({"v": 1, "kid": kid, "seen": list(keep),
                "mark_hour_ms": int(state["mark_hour_ms"]) if failed else hour_now})
    log(f"检查完成，新成交 {len(new)} 条，已发送 {len(sent)} 条")
    return 2 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    try:
        rc = run("test" if a.test else "live")
    except OKXError as e:
        log(f"OKX 读取失败: {e}")       # HTTP status / okx code / error type only
        rc = 3
    except Exception as e:
        log(f"运行出错: {type(e).__name__}")
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
