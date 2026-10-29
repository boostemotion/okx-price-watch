# okx-price-watch

GitHub Actions port of `/workspace/okx/bin/price_watch.py` (Hermes's read-only OKX price alerts).

**This repo is public.** Positions live only in the Actions secret `POSITIONS_JSON`; run logs print only
counts (`检查完成，读取价格 5/5，命中 N 条，已发送 N 条` and `webhook HTTP 200`); the committed state file stores
position dedupe as opaque hashes. Never commit positions, entry/stop/size/side, or webhook values.
Public OKX market data only: no OKX key, never trades. Each run is one cycle; on a hit it POSTs JSON
to Hermes's webhook. After 2026-11-22 23:59:59 UTC+8 runs exit without alerting.

## Files
| path | what |
|---|---|
| `watch.py` | one cycle (stdlib only). `--dry-run` (local debugging only, prints details) no POST, no state write. `--test` sends one `[TEST]` webhook. |
| `config/params.json` | thresholds (Pax), cooldowns, further-move %, end date. Change by commit. |
| `state/alert-state.json` | public dedupe memo, committed back only when it changes. `moves`: last alert time + market price per symbol/direction; `positions`: `h:<sha256[:16]>` of instId\|side\|entry\|sl → time only. |
| `.github/workflows/watch.yml` | `workflow_dispatch` (input `test_mode`) + `schedule */5`; `concurrency` serialises runs. |
| `server/install.sh`, `server/paste.txt` | Shanghai server: systemd timer that dispatches the workflow every 5 min. |
| `tests/test_watch.py` | mocked-price tests: `python3 -m unittest -v tests/test_watch.py` |

## Rules (unchanged from the box script)
- 15m candles. Move alert when the last two CLOSED bars each moved ≥ threshold over 1h (same direction).
  Threshold = US regular hours (09:30–16:00 New York, Mon–Fri) or off-hours value from `thresh_pct`.
- Re-alert same instrument+direction only after cooldown (60 min US / 120 min off) or a further move
  ≥ 2% (SKUU 3%) beyond the last alerted price.
- Half-to-stop: alert once when price has covered ≥ 50% of entry→stop distance; re-arms when entry/sl changes.
- A failed POST is not deduped, so the next run retries it (run turns red).

## Updating positions / stop-loss (Hermes)
Only via the Actions **secret** `POSITIONS_JSON` (same shape as the box `state/positions.json`):
`{"positions":[{"instId":"<INST>","side":"long|short","size":<n>,"entry":<px>,"sl":<px>}]}`.
Unset or `{"positions":[]}` → the half-to-stop rule is skipped. From the box (never echo the value):
`gh secret set POSITIONS_JSON -R boostemotion/okx-price-watch < /workspace/okx/state/positions.json`
(or REST `PUT /repos/boostemotion/okx-price-watch/actions/secrets/POSITIONS_JSON`, needs Secrets: write).
Takes effect on the next run. Changing entry/sl re-arms that position's alert.

## Manual setup (Elian)
1. Repo secrets (Settings → Secrets and variables → Actions): `HERMES_WEBHOOK_URL`, `HERMES_WEBHOOK_KEY`,
   `POSITIONS_JSON`.
   Optional variable `HERMES_WEBHOOK_KEY_HEADERS` (default `Authorization,X-Automation-Key`, same as today).
2. Dispatch token for the server: github.com → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate. Resource owner `boostemotion`; Expiration: e.g. 2026-11-30;
   Repository access: **Only select repositories → okx-price-watch**; Repository permissions:
   **Actions: Read and write** (Metadata: Read-only is added automatically). Nothing else.
3. Tencent Cloud console → 云服务器 → 执行命令: paste `server/paste.txt` after replacing `PASTE_TOKEN_HERE`.
   Check: `journalctl -u okx-dispatch -n 20`. Remove: `systemctl disable --now okx-dispatch.timer`.
   The dispatcher turns itself off after 2026-11-23 00:00 UTC+8.
4. After 2026-11-22, disable the workflow (Actions → okx-price-watch → Disable) to stop the schedule.

## Actions minutes
Public repo: standard GitHub-hosted runners are free. (Private on the Free plan would exhaust 2,000 min in days.)
