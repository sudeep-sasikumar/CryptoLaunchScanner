# Launch Radar

A scanner that watches every new token launch on pump.fun, PONS, StonkFun and the other major
launchpads, measures each one against ~130 factors, calls the few that look like runners, then
checks what actually happened and retrains itself on the result.

**Signals only. It never trades, holds no keys, and is not financial advice.** Most new launches go
to zero; a call is a statistical edge at best, never a promise.

---

## 1. What it does, in one loop

```
launch appears ─► watched trade by trade ─► SNAPSHOT at 1, 3, 10, 30 min   (5, 15, 45 min off pump.fun)
                                               │  every factor, as known at that second
                                               ├─► hard vetoes (honeypot, bundle, dev dump ...)
                                               ├─► score: scorecard 0-100, or model probability once trained
                                               ├─► top slice only ─► CALL on Telegram
                                               └─► LABEL: did price reach 2x before falling 50%?  (up to 6 h)
every 12 h: retrain on all labels ─► test on the newest unseen data ─► promote only if better
```

Every active launch gets snapshots and labels, **not just the ones that were called**. That is what
lets it learn from the runners it missed and the rugs it avoided.

## 2. What "a runner" means here

A call is a **hit** when price reaches **2x the call price before dropping to half of it**, within
6 hours. The call price is assumed 3% worse than the last trade (you never get the last price).
Price is averaged over 15-second bars, so a single freak trade cannot fake a 2x. All of this is in
`config.yaml` under `label:`.

## 3. Your factor list → how each one is captured

| # | Your factor | How the scanner measures it |
|---|---|---|
| 1 | No bundles | Supply bought **in the creation slot** by non-dev wallets (`launch_slot0_*`), plus later same-slot buying clusters and identical-size buys (`launch_cluster_pct`, `launch_same_size_share`). Vetoed while those wallets still hold over 25% of supply. |
| 2 | Few snipers | Buys in the first 5 slots (~2 s) after creation (`launch_sniper_*`), and whether they still hold. |
| 3 | Few phishing wallets | Tokens sold by wallets that **never bought** (they arrived by transfer): `launch_phantom_sold_pct`. RugCheck insider networks on candidates. |
| 4 | Fair launch | 1-3 together, plus dev's own buy and time to first outside buy. |
| 5, 6 | Unique product / flywheel / utility / viral meme | Optional Claude rating of name, ticker, description, links: `llm_viral`, `llm_original`, `llm_utility`, `llm_effort`. Copycats launched *after* the coin (`id_name_after_n`) measure spread directly. |
| 7 | Smart wallets | **Learned, not bought.** Every finished token credits its first 40 buyers; wallets that are early in winners far more than average, with realised profit, become smart (`wq_smart_n`, `wq_smart_holding_n`). |
| 8 | Clean contract | pump.fun coins are fixed-supply with no tax by construction. Elsewhere: GoPlus honeypot / tax / owner / mint / blacklist / verified (`sec_*`); RugCheck on Solana (`rc_*`). Hard vetoes. |
| 9 | Few same-name tokens | Every name and ticker ever seen is counted (`id_name_prev_n`, `id_sym_prev_n`, burst in last 10 min). |
| 10 | Image not reused | Perceptual hash of the image; counts earlier tokens with the same picture (`id_img_prev_n`). Same for metadata link. |
| 11, 12 | X profile / TG group | From token metadata: present or not, own account vs. a random post, reused by earlier tokens, Telegram member count. |
| 13 | LP burned | GoPlus LP-locked % on EVM. On pump.fun the curve holds liquidity until graduation (burned by design). |
| 14 | Dev not sold | `launch_dev_sold_pct`, `launch_dev_hold_pct`. Vetoed when the dev has sold most of the bag. |
| 15, 16 | Top-10 %, distribution | Rebuilt from every trade: top 1/5/10 %, concentration index, median holder, holders per minute. |
| 17 | Healthy holder growth | New buyers per minute, share of buyers who already left, repeat buyers. |
| 18 | Insider wallets | Creation-slot buyers, phantom sellers, RugCheck insider graph. |
| 19 | Social chatter | Partly: Telegram members, DexScreener paid profile / boosts, copycat count. **Live X mention counts need a paid X API and are not included.** |
| 20 | Rug wallets | Learned: wallets that keep buying in the creation slot of coins that go nowhere, or keep dumping big bags (`wq_toxic_n`). Creator history (`cr_*`). |
| 21 | Chart | Drawdown from high, trend slope and steadiness, higher lows, returns over 30 s / 60 s. Full price path stored per token. |
| 22 | Volume | Buy/sell split, acceleration, volume vs. mcap, trade sizes, last-minute share. |
| 23 | How failures dump | Every tracked token is classified at the end: dev dump, bundle dump, whale dump, fast fade, slow bleed, never started - with time-to-peak, time top→half, who sold. Dashboard tab "How launches die". |

Added beyond your list: market mood (launches per minute, recent hit rate, graduations per hour),
time of day, quote currency (pump.fun coins can now be paired with SOL, USDC, PUMP or other coins),
mayhem / cashback / holder-reward modes, creator's launch count in 24 h, bonding-curve progress.

## 4. How it improves itself

1. **Labels.** Each snapshot is followed until it resolves. Nothing is assumed.
2. **Retrain** every 12 h (once there are 1,500 labelled snapshots and 60 winners - roughly the first
   day on pump.fun). A gradient-boosted model per family (`pump`, `dex`).
3. **Honest test.** History is split by time: fit on the oldest 70%, test on the newest 15% it never
   saw. The new model replaces the old one only if it clearly beats the hand scorecard, beats luck
   (99% of random rankings), and is no worse than the model in use.
4. **Threshold moves too.** The call cut-off is set where the newest data shows the target hit rate
   (`call.target_precision`, default 40%).
5. **Wallet and creator reputations** update after every finished token.
6. **Readable output.** The dashboard tab "What it has learned" shows, for every factor, the 2x rate
   in each value range - so you can see what matters without trusting a model.

Until the first model is promoted, the hand-weighted scorecard in `radar/scoring.py` decides and the
scanner calls the top 2% of scores from the last 24 h.

## 5. Data sources (all free; verified live on 2026-10-14)

| Source | Used for | Limit seen |
|---|---|---|
| Solana RPC websocket, pump.fun program logs | Every pump.fun create / trade / graduation, with slot numbers | ~350 messages/s, ~85 GB/day download on the public node |
| GeckoTerminal `new_pools`, `trades` | Discovering launches off pump.fun (Solana, Robinhood Chain, BNB, Base); wallet-level trades | ~5 calls/min free (measured), 30 with a free CoinGecko demo key |
| DexScreener `tokens/v1` | Price / volume / txns for 30 tokens per call, any chain; paid profiles | 300/min documented |
| GoPlus | EVM contract safety (Robinhood Chain = chain 4663) | no key |
| RugCheck | Solana holders, insiders, risk flags | 15/min |
| Token metadata + t.me | Socials, image, Telegram members | - |

PONS is on Robinhood Chain (GeckoTerminal dex ids `pons-v2`, `pons-dot-family`); StonkFun deploys
through Raydium LaunchLab on Solana. Other launchpads are mapped in `config.yaml` → `launchpads`.

## 6. Run it on this PC

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env
.venv\Scripts\python main.py
```

Dashboard: http://localhost:8010 (no login on localhost). Without Telegram settings, calls are
printed to the console. `python main.py --minutes 5` runs a 5-minute smoke test;
`.venv\Scripts\pytest -q` runs the tests.

## 7. Running on the VPS (same pattern as Ignition & Coil)

1. Repository `LaunchRadar`, branch `master`. Every push runs the tests, builds the image and
   publishes `ghcr.io/sudeep-sasikumar/launchradar:latest` (GitHub Actions).
2. On GitHub: Packages → launchradar → make it public, so the VPS can pull it without a login
   (the same as `ignitionandcoil`).
3. Hostinger → VPS → Docker Manager → Compose → Compose from URL:
   `https://raw.githubusercontent.com/sudeep-sasikumar/LaunchRadar/master/docker-compose.yaml`,
   project name `launch-radar`.
4. Enter `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_PASSWORD` (and any optional keys
   listed at the top of `docker-compose.yaml`), then Deploy.
5. Dashboard: `https://srv1479428.hstgr.cloud/radar/` - it shares the hostname and certificate
   that Ignition & Coil already uses. Ignition & Coil keeps everything outside `/radar/`.

Data (database, models, logs) lives in the `radar_data` volume and survives redeploys. A redeploy
restarts the scanner: snapshots still being followed at that moment are set aside, not trained on.

**Check before deploying:** the pump.fun feed downloads about 85 GB/day (~2.5 TB/month). Confirm
your VPS plan's bandwidth allowance. Expect a few hundred MB of RAM.

**Storage.** Only what the learner and the dashboard use is written, and it is capped at 20 GB
(`storage.max_gb`). Measured live: about 450 bytes per snapshot (factor names are stored once per
layout, values rounded to 4 significant digits). At ~9,000 snapshots a day and the default
retention, the whole database settles around 2 GB:

| Kept | For | How long |
|---|---|---|
| Snapshots (every factor + outcome) | training, lift tables | 120 days (called ones: 365) |
| Calls | scoreboard | 365 days |
| Tokens that got a snapshot: how they ended + thinned price path | failure patterns | 30 days |
| Wallet and creator counters | smart / toxic wallets, creator record | seen < 4 times: 45 days; any: 120 days idle |
| Name / symbol / image / social counters | uniqueness factors | 10 days idle |

Never stored: raw trades, launches that never became active, unthinned price bars. Pruning runs
hourly in small batches. If the database still passes 90% of the cap, the oldest snapshots are
dropped first.

## 8. Settings worth knowing (`config.yaml`)

| Setting | Default | Meaning |
|---|---|---|
| `label.target` / `label.stop` | 2.0 / 0.5 | What counts as a hit |
| `call.top_pct` | 2.0 | Before a model exists: call the top X% of scores (scorecard 75+ until 100 scores exist) |
| `call.target_precision` | 0.40 | With a model: hit rate the threshold aims for |
| `call.max_per_hour` | 10 | Alert cap |
| `call.mcap_usd` | 4.5k - 400k | Mcap range a call may be in |
| `gates.*` | | Vetoes. Safety ones always apply; the ones marked (*) step aside once a model is in use |
| `learn.retrain_hours` | 12 | Retrain cadence |
| `storage.max_gb` | 20 | Disk cap for database + models |

Override without editing the file: `RADAR_OVERRIDES="call.top_pct=1;label.target=3"`.

## 9. Known limits

- **First day = scorecard only.** The hand weights are a starting opinion, not a tested edge.
- **Off-pump.fun launches are seen about a minute late** and less deeply (periodic snapshots, not
  every trade). On the free GeckoTerminal limit a busy Solana minute can hide some non-pump pools;
  the free CoinGecko demo key fixes most of that.
- **No X/Twitter mention counts** (paid API).
- **The public Solana RPC can drop.** Tokens live during a drop are set aside and never trained on.
  A free Helius key in `SOLANA_WS_URL` is steadier.
- **A restart loses open tracking.** Unfinished snapshots are set aside, open calls marked expired.
- **Holder data ignores transfers** on pump.fun (it is rebuilt from trades). Phantom sells and
  RugCheck cover part of the gap.
- **Graduated pump.fun coins** are followed by 30-second price polls, so a very brief spike can be missed.
- **Liquidity is thin at these market caps.** A hit on paper is not a fill; size matters.

## 10. Layout

```
main.py                  entry point
config.yaml              every threshold
radar/engine.py          the loop: snapshots, calls, labels, retraining
radar/state.py           per-token state rebuilt from trades; label logic
radar/features.py        all factors
radar/scoring.py         vetoes + day-one scorecard
radar/model.py           learner: time-split test, promotion, threshold, lift tables
radar/wallets.py         smart / toxic wallet and creator reputation
radar/sources/           pump.fun stream + decoder, GeckoTerminal, DexScreener, GoPlus, RugCheck, metadata
radar/llm.py             optional Claude meme / narrative rating
radar/notify.py          Telegram
radar/web/               dashboard
tests/                   27 tests
```
