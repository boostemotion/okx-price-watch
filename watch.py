#!/usr/bin/env python3
"""OKX price-alert watcher, one cycle per run (GitHub Actions port of okx/bin/price_watch.py).

Public OKX market data only (no API key, never trades). On a hit, POSTs JSON to Hermes's webhook.
The repo is PUBLIC: Actions logs print only counts ("检查完成，命中 N 条"), never prices of held
positions, entry/sl/size/side, or webhook values. Position dedupe keys are stored as opaque hashes.

  python watch.py             one cycle: alert on hits, update state/alert-state.json
  python watch.py --dry-run   one cycle, print prices + decisions, no POST, no state write
  python watch.py --test      print prices, then send ONE webhook marked [TEST] (no state write)

Env: HERMES_WEBHOOK_URL, HERMES_WEBHOOK_KEY (secrets), HERMES_WEBHOOK_KEY_HEADERS (optional,
default "Authorization,X-Automation-Key"), POSITIONS_JSON (secret; unset -> half-to-stop rule
skipped), OKX_BASES (optional comma list of API hosts to try in order).
Stdlib only.
"""
import argparse, datetime as dt, hashlib, json, os, sys, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

BJT = ZoneInfo("Asia/Shanghai")
NYT = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS = os.path.join(HERE, "config", "params.json")
STATE = os.path.join(HERE, "state", "alert-state.json")
UA = "Mozilla/5.0"
BAR_MS = 15 * 60 * 1000
TEST_TEXT = "[TEST] 测试消息，不是真实报警，请勿下单"


def now_bjt():
    return dt.datetime.now(BJT)


def log(msg):
    print(f"{now_bjt():%Y-%m-%d %H:%M:%S} UTC+8 {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def load_params():
    p = load_json(PARAMS, None)
    if p is None:
        sys.exit(f"missing {PARAMS}")
    p["end"] = dt.datetime.fromisoformat(p["end_bjt"])
    return p


def session(t):
    """'us' during NYSE regular hours 09:30-16:00 New York Mon-Fri; holidays not modelled."""
    ny = t.astimezone(NYT)
    if ny.weekday() >= 5:
        return "off"
    mins = ny.hour * 60 + ny.minute
    return "us" if 570 <= mins < 960 else "off"


# ---------- webhook ----------
def webhook_cfg():
    g = lambda k, d=None: os.environ.get(k) or d
    return {"url": g("HERMES_WEBHOOK_URL"), "key": g("HERMES_WEBHOOK_KEY"),
            "headers": g("HERMES_WEBHOOK_KEY_HEADERS", "Authorization,X-Automation-Key")}


def post_alert(body, cfg):
    """One try, 8 s timeout. True on HTTP 2xx. Never prints URL or key."""
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


# ---------- OKX public data ----------
def okx_get(path, params):
    bases = [b.strip() for b in os.environ.get(
        "OKX_BASES", "www.okx.com,us.okx.com,eea.okx.com").split(",") if b.strip()]
    last = None
    for b in bases:
        url = f"https://{b}{path}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.load(r)
            if j.get("code") != "0":
                raise RuntimeError(f"okx code {j.get('code')} {j.get('msg')}")
            return j
        except Exception as e:
            last = f"{b}: {type(e).__name__} {str(e)[:120]}"
    raise RuntimeError(last)


def get_candles(inst_id, limit=12):
    """List oldest->newest of {ts, close, confirm} for 15m bars (same source as original)."""
    j = okx_get("/api/v5/market/candles", {"instId": inst_id, "bar": "15m", "limit": str(limit)})
    out = [{"ts": int(c[0]), "close": float(c[4]), "confirm": c[8] == "1"} for c in j["data"]]
    return sorted(out, key=lambda c: c["ts"])


def move_metrics(candles):
    """1h change now (latest vs close 4 bars earlier) and for the last two CLOSED 15m bars."""
    by_ts = {c["ts"]: c["close"] for c in candles}
    closed = [c for c in candles if c["confirm"]]
    if len(closed) < 2:
        return None
    cur = candles[-1]

    def chg(ts, px):
        base = by_ts.get(ts - 4 * BAR_MS)
        return None if not base else (px / base - 1) * 100

    c1, c2 = closed[-1], closed[-2]
    return {"price": cur["close"], "now": chg(cur["ts"], cur["close"]),
            "c1": chg(c1["ts"], c1["close"]), "c2": chg(c2["ts"], c2["close"])}


# ---------- positions / state ----------
def load_positions():
    raw = os.environ.get("POSITIONS_JSON", "").strip()
    if not raw:
        return []
    try:
        d = json.loads(raw)
    except Exception:
        log("WARN POSITIONS_JSON 无法解析，跳过半止损规则")
        return []
    if isinstance(d, dict):
        d = d.get("positions", d.get("data", [d] if "instId" in d else []))
    if isinstance(d, dict):
        d = [d]
    out = []
    for p in d if isinstance(d, list) else []:
        try:
            out.append({"instId": str(p["instId"]), "side": str(p["side"]).lower(),
                        "size": p.get("size"), "entry": float(p["entry"]), "sl": float(p["sl"])})
        except Exception:
            continue
    return out


def load_state():
    s = load_json(STATE, {}) or {}
    s.setdefault("moves", {}); s.setdefault("positions", {})
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE)


def pos_key(p):
    """Opaque dedupe key: the committed state file is public, so never store entry/sl/size/side."""
    raw = f"{p['instId']}|{p['side']}|{p['entry']}|{p['sl']}"
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def body(P, inst_id, direction, change_pct, price, reason, sess, t, window="1h", **extra):
    tk = next((k for k, v in P["instruments"].items() if v == inst_id), inst_id.split("-")[0])
    b = {"instId": inst_id, "ticker": tk, "direction": direction,
         "change_pct": None if change_pct is None else round(change_pct, 2),
         "window": window, "price": price, "reason": reason, "session": sess,
         "timestamp": t.strftime("%Y-%m-%d %H:%M:%S UTC+8"), "source": "github-actions"}
    b.update(extra)
    return b


# ---------- one cycle ----------
def evaluate(P, state, metrics, positions, t, errors=()):
    """Pure decision step. Returns (alerts, summary, live_position_keys).
    alerts: list of (key, bucket, body, price)."""
    sess = session(t)
    tk_of = {v: k for k, v in P["instruments"].items()}
    alerts, summary = [], list(errors)
    fmt = lambda v: "na" if v is None else f"{v:+.2f}%"
    for tk, inst in P["instruments"].items():
        m = metrics.get(inst)
        if not m:
            continue
        th = P["thresh_pct"][tk][0 if sess == "us" else 1]
        summary.append(f"{tk} {m['price']} 1h={fmt(m['now'])} c1={fmt(m['c1'])} c2={fmt(m['c2'])} th={th}%")
        if m["c1"] is None or m["c2"] is None:
            continue
        for direction, sign in (("up", 1), ("down", -1)):
            if not (sign * m["c1"] >= th and sign * m["c2"] >= th):
                continue
            key = f"{inst}|{direction}"
            last = state["moves"].get(key)
            if last:
                age_min = (t.timestamp() - last["t"]) / 60
                further = (m["price"] / last["price"] - 1) * 100 * sign
                need = P.get("further_pct", {}).get(tk, P.get("further_pct_default", 2.0))
                if age_min < P["cooldown_min"][sess] and further < need:
                    summary.append(f"{tk} {direction} suppressed(cooldown {age_min:.0f}m, further {further:+.2f}%)")
                    continue
            chg = m["now"] if m["now"] is not None else m["c1"]
            alerts.append((key, "moves", body(P, inst, direction, chg, m["price"], "price_move", sess, t,
                                              threshold_pct=th, confirm_c1=round(m["c1"], 2),
                                              confirm_c2=round(m["c2"], 2)), m["price"]))

    for p in positions:
        m = metrics.get(p["instId"])
        if not m:
            continue
        price, e, sl, side = m["price"], p["entry"], p["sl"], p["side"]
        tk = tk_of.get(p["instId"], p["instId"].split("-")[0])
        if side in ("long", "buy"):
            if sl >= e:
                summary.append(f"pos {tk} long skip(sl>=entry)"); continue
            covered, direction = (e - price) / (e - sl), "down"
        elif side in ("short", "sell"):
            if sl <= e:
                summary.append(f"pos {tk} short skip(sl<=entry)"); continue
            covered, direction = (price - e) / (sl - e), "up"
        else:
            continue
        pkey = pos_key(p)
        summary.append(f"pos {tk} {side} covered={covered*100:.0f}%")
        if covered >= 0.5 and pkey not in state["positions"]:
            alerts.append((pkey, "positions", body(P, p["instId"], direction, (price / e - 1) * 100, price,
                                                   "half_to_stop", sess, t, window="since_entry",
                                                   side=side, entry=e, sl=sl,
                                                   stop_distance_covered_pct=round(covered * 100, 1)), price))
    live = {pos_key(p) for p in positions}
    return alerts, summary, live


def fetch_metrics(P, positions):
    inst_ids = list(P["instruments"].values()) + sorted({p["instId"] for p in positions}
                                                        - set(P["instruments"].values()))
    metrics, errors = {}, []
    for inst in inst_ids:
        try:
            m = move_metrics(get_candles(inst))
            if m:
                metrics[inst] = m
        except Exception as e:
            # only main-list symbols may be named in errors; extra (held) symbols stay anonymous
            name = inst if inst in P["instruments"].values() else "position-symbol"
            errors.append(f"{name}=ERR({str(e)[:160]})")
    return metrics, errors


def run(mode, P=None, t=None, fetch=fetch_metrics, post=post_alert):
    """mode in {'live','dry','test'}. Returns process exit code."""
    P = P or load_params()
    t = t or now_bjt()
    if t > P["end"] and mode != "test":
        log(f"past end {P['end_bjt']}; no alerts")
        return 0
    state = load_state()
    positions = load_positions()
    metrics, errors = fetch(P, positions)
    alerts, summary, live = evaluate(P, state, metrics, positions, t, errors)
    n_ok = sum(1 for i in P["instruments"].values() if i in metrics)
    n_all = len(P["instruments"])
    rc = 0 if n_ok == n_all else 3  # fetch failure -> visible red run
    if n_ok < n_all:
        for e in errors:
            if not e.startswith("position-symbol"):
                log("ERR " + e)  # OKX/network error text only

    if mode == "dry":  # local debugging only; the workflow never runs --dry-run
        print(f"cycle session={session(t)} alerts={len(alerts)} | " + "; ".join(summary))
        for _, _, b, _ in alerts:
            print("WOULD ALERT:", json.dumps(b, ensure_ascii=False))
        return rc
    if mode == "test":
        tb = body(P, "SKUU-USDT-SWAP", "up", 0.0, None, "test", session(t), t,
                  test=True, message=TEST_TEXT,
                  prices={k: v["price"] for k, v in metrics.items()})
        log(f"测试模式：读取价格 {n_ok}/{n_all}")
        ok = post(tb, webhook_cfg())
        log("测试 webhook " + ("OK" if ok else "FAILED"))
        return 0 if ok else 1

    cfg = webhook_cfg()
    state["positions"] = {k: v for k, v in state["positions"].items() if k in live}
    sent = 0
    for key, bucket, b, price in alerts:
        if post(b, cfg):
            sent += 1
            # moves memo keeps the public market price (needed for the further-move rule);
            # position memo keeps only the time (a price there would hint at the stop level)
            state[bucket][key] = ({"t": round(t.timestamp()), "price": price} if bucket == "moves"
                                  else {"t": round(t.timestamp())})
        else:
            rc = 2  # not deduped -> retried next run
    save_state(state)
    log(f"检查完成，读取价格 {n_ok}/{n_all}，命中 {len(alerts)} 条，已发送 {sent} 条")
    return rc


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--test", action="store_true")
    a = ap.parse_args()
    try:
        rc = run("dry" if a.dry_run else "test" if a.test else "live")
    except Exception as e:  # no traceback: messages could carry config values
        log(f"运行出错: {type(e).__name__}")
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
