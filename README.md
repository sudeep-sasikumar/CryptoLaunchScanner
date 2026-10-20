# Launch Radar

A scanner that watches every new token launch on pump.fun, PONS, StonkFun and the other major
launchpads, measures each one against ~130 factors, calls the few that look like runners, then
checks what actually happened and retrains itself on the result.

**Signals only. It never trades, holds no keys, and is not financial advice.** Most new launches go
to zero; a call is a statistical edge at best, never a promise.

---

## 1. What it does, in one loop

```
launch appears ─► watched ─► SNAPSHOT at 1, 3, 10, 30 min   (the same ages on every launchpad)
                                               │  every factor, as known at that second
                                               ├─► hard vetoes (honeypot, bundle, dev dump ...)
                                               ├─► score: scorecard 0-100, or model probability once trained
                                               ├─► top slice only ─► CALL on Telegram
                                               └─► LABEL: did price reach 2x before falling 50%?  (up to 6 h)
every 12 h: retrain on all labels ─► test on the newest unseen data ─► promote only if better
```

Every active launch gets snapshots and labels, **not just the ones that were called**. That is what
lets it learn from the runners it missed and the rugs it avoided.

**Every launchpad is judged the same way**: the same ages, one scorecard, one veto list, one learned
model (with the launchpad as one of its factors). pump.fun coins get the same contract-safety checks
as everything else; other launchpads get the same launch-fairness and demand factors as pump.fun.
What differs is only how much can be seen: pump.fun is read trade by trade from the chain; other
launchpads are found through GMGN's launch feed (seconds after creation) or GeckoTerminal, and
priced every 15 s via DexScreener. Their holder picture (holders, top 10, dev, insiders, bundlers,
snipers, phishing) comes from whichever holder-data services have a key: GMGN, Codex, Solana
Tracker, plus keyless GeckoTerminal and RugCheck. A factor no source can measure scores neutral.

## 2. What "a runner" means here

A call is a **hit** when price reaches **2x the call price before dropping to half of it**, within
6 hours. The call price is assumed 3% worse than the last trade (you never get the last price).
Price is averaged over 15-second bars, so a single freak trade cannot fake a 2x. All of this is in
`config.yaml` under `label:`.

## 3. Your factor list → how each one is captured

| # | Your factor | How the scanner measures it |
|---|---|---|
| 1 | No bundles | pump.fun: supply bought **in the creation slot** by non-dev wallets (`launch_slot0_pct`), later same-slot clusters and identical-size buys. Elsewhere: GMGN's bundler share (`gm_bundler_pct`), which is also kept for pump.fun coins. Vetoed while bundle/sniper wallets still hold over 25%. |
| 2 | Few snipers | pump.fun: buys in the first 5 slots (~2 s) and whether they still hold. Elsewhere: GMGN's top-70 sniper holding (`gm_sniper_hold_pct`) and sniper count. |
| 3 | Few phishing wallets | GMGN's phishing ("entrapment") share: supply held by wallets that received tokens instead of buying them (`gm_phishing_pct`). Scored, and vetoed above 30%. pump.fun also keeps its own count of tokens sold by wallets that never bought. |
| 4 | Fair launch | 1-3 together, plus dev's own buy and time to first outside buy. |
| 5, 6 | Unique product / flywheel / utility / viral meme | Optional Claude rating of name, ticker, description, links: `llm_viral`, `llm_original`, `llm_utility`, `llm_effort`. Copycats launched *after* the coin (`id_name_after_n`) measure spread directly. |
| 7 | Smart wallets | Two views: GMGN's smart-money and KOL counts (`gm_smart_n`, `gm_kol_n`), and the scanner's **own learned** smart wallets - every finished token credits its first 40 buyers; wallets early in winners far more than average, with realised profit, become smart (`wq_smart_n`). |
| 8 | Clean contract | Every token, pump.fun included: GMGN security (honeypot, tax, renounced, blacklist, verified; mint and freeze authority on Solana) plus GoPlus as a second, independent check. Safety vetoes, and no call unless a check answered. |
| 9 | Few same-name tokens | Every name and ticker ever seen is counted (`id_name_prev_n`, `id_sym_prev_n`, burst in last 10 min). |
| 10 | Image not reused | Our own perceptual hash of the image and GMGN's "avatar reused" count; also reused X / Telegram / website links (`gm_*_dup_n`). |
| 11, 12 | X profile / TG group | Present or not, the coin's own account or someone's post, reused by earlier tokens, Telegram members, and GMGN's X follower count. |
| 13 | LP burned | GMGN burn / lock share (`gm_burnt_pct`, e.g. "Burnt 95%"); on a bonding curve the launchpad holds the liquidity, which counts as locked. |
| 14 | Dev not sold | pump.fun: share of the dev's own bag sold (`launch_dev_sold_pct`). Every launchpad: GMGN dev holding and whether the dev has closed the position (`gm_dev_hold_pct`, `gm_dev_closed`). |
| 15, 16 | Top-10 %, distribution | pump.fun: rebuilt from every trade (top 1/5/10 %, concentration, median holder). Everywhere: GMGN holders and top 10, bot and fresh-wallet shares (`gm_bot_pct`, `gm_fresh_pct`). |
| 17 | Healthy holder growth | New buyers per minute, share of buyers who already left, repeat buyers. |
| 18 | Insider wallets | GMGN's insider ("rat trader") share (`gm_insider_pct`), scored and vetoed above 30%; creation-slot buyers; RugCheck insider graph when available. |
| 19 | Social chatter | GMGN: X followers, Telegram call count, call-outs, KOL holders, DexScreener paid profile / boosts / CTO; copycats launched after the coin. Live X mention counts are not available. |
| 20 | Rug wallets | Learned toxic wallets (`wq_toxic_n`) and creator history: ours, plus GMGN's count of coins this creator made and how many graduated. A creator with 50+ coins and under 5% graduating is vetoed as a coin farm. |
| 21 | Chart | Drawdown from high, trend slope and steadiness, higher lows, returns over 30 s / 60 s. Full price path stored per token. |
| 22 | Volume | Buy/sell split, acceleration, volume vs. mcap, trade sizes, last-minute share. |
| 23 | How failures dump | Every tracked token is classified at the end: dev dump, bundle dump, whale dump, fast fade, slow bleed, never started - with time-to-peak, time top→half, who sold. Dashboard tab "How launches die". |

Added beyond your list: GMGN's rug ratio and wash-trading flag (wash trading is a safety veto),
dev funding age, private-vault holdings, page visits, "hot" level, DexScreener ad / trending
payments; market mood (launches per minute, recent hit rate, graduations per hour), time of day,
quote currency (pump.fun coins can be paired with SOL, USDC, PUMP or other coins), mayhem /
cashback / holder-reward modes, creator's launch count in 24 h, bonding-curve progress.

## 4. How it improves itself

**Since 2026-10-20 the model only changes when you ask.** Automatic retraining is off
(`learn.auto_retrain: false`, `AUTO_RETRAIN` in Hostinger): the model in use and its call threshold stay
fixed, while every coin is still recorded and graded. A retrain is started from the PC:

| To... | Run (PowerShell) |
|---|---|
| see what is in use and how the last retrain went | `.venv\Scripts\python.exe tools\retrain.py --status` |
| train a candidate and compare it, changing nothing | `.venv\Scripts\python.exe tools\retrain.py` |
| ... and switch to it if it passes the tests below | `.venv\Scripts\python.exe tools\retrain.py --promote` |

Still automatic (bookkeeping, not changes to the rules): wallet and creator records, liquidity look-ups and
the creator-pull block, the conviction ranking against the last 24 h, the daily research files.

1. **Labels.** Each snapshot is followed until it resolves. Nothing is assumed.
2. **Retrain** on request (with `AUTO_RETRAIN=true`: every 12 h, once there are 1,500 labelled snapshots
   and 60 winners). One gradient-boosted model for all launchpads; the launchpad, chain and data source
   are factors it can use if they matter.
3. **Honest test.** History is split by time: fit on the oldest 70%, test on the newest 15% it never
   saw. The new model replaces the old one only if it clearly beats the hand scorecard, beats luck
   (99% of random rankings), and is no worse than the model in use.
4. **Threshold moves too.** The call cut-off is set where the newest data shows the target hit rate
   (`call.target_precision`, default 50%; never below `call.min_prob`).
5. **Wallet and creator reputations** update after every finished token.
6. **Readable output.** The dashboard tab "What it has learned" shows, for every factor, the 2x rate
   in each value range - so you can see what matters without trusting a model.
7. **Daily copy + traits report on this PC** (same pattern as Ignition & Coil, below).

### Copying the VPS data to this PC and studying it (daily, automatic)

The VPS keeps learning on its own (steps 1-6). On top of that, `tools\sync_research.py` signs in to
the dashboard (`research.sync_url` in `config.yaml`), downloads one file per UTC day into
`var\research\vps\` - every finished snapshot with all its factors and what happened next, that day's
calls, and how tokens ended - then writes **`var\research\report-<date>.md`**: what the coins that ran
2x / 4x / 10x / 100x had in common, which launchpads they came from, whether the scorecard / model
ranks them on top, and a few simple rules checked on newer tokens than they were learned on.

- **Password:** uses `DASHBOARD_PASSWORD` from this PC's `.env` (create the file with that one line,
  same password as the dashboard). Never shown, logged, or put on a command line.
- **Safe replacement:** the newest 3 days are always refreshed (outcomes take up to 6 h, liquidity checks 48 h);
  a file is only replaced after the new copy passes a full integrity check.
- **Schedule:** run `powershell -ExecutionPolicy Bypass -File tools\install_daily_sync.ps1` once. It
  registers the task **"Launch Radar research sync"**: every day at 09:30, or as soon as the PC is on;
  under your account, only while you're logged in; stores no password.
- **Log:** `var\logs\research_sync.log`. Downloads stay on this PC (`var\` is not in git).

| To... | Run (PowerShell) |
|---|---|
| sync + report now | `.venv\Scripts\python.exe tools\sync_research.py` |
| report only (e.g. last 7 days, at the 3-min snapshot) | `.venv\Scripts\python.exe tools\analyze.py --days 7 --checkpoint 180` |
| run the scheduled task now | `Start-ScheduledTask -TaskName "Launch Radar research sync"` |
| remove it | `Unregister-ScheduledTask -TaskName "Launch Radar research sync" -Confirm:$false` |

Until the first model is promoted, the hand-weighted scorecard in `radar/scoring.py` decides and the
scanner calls the top 2% of scores from the last 24 h.

**Scorecard rebalanced 2026-10-16** from 3 days of outcomes: weights now follow how well each item told
2x runners apart (money flowing in, buyers staying, many holders spread across wallets, fresh highs,
fresh creators, early buyers with a winning record). "Top 10 not concentrated", "no bundle", "has
socials" and "original name" pointed the wrong way and were dropped from the score (the vetoes still
apply). On a day it was not tuned on, separation rose from 0.59 to 0.69 and the top 5% 2x'd 26% of the
time instead of 16%; the learned model (0.78) still decides once trained.

**Pull-proof calls only (2026-10-17).** On 14-16 Oct, 87-100% of raw Robinhood Uniswap v2/v3/v4 and
Meteora DAMM v2 pools were empty within two days, while pump.fun, PONS and four.meme pools kept their
liquidity. Calls are therefore limited to coins whose liquidity cannot be pulled: still on a bonding
curve, graduated from a launchpad that burns or locks the LP (pump.fun, PONS, four.meme), or with 95%+ of
the LP locked. Every coin is still scored and labelled, so the model keeps learning from all of them.
Set `PULL_PROOF_ONLY=false` to call every launchpad again.

**Learning who pulls.** Each DEX coin that held at least $1,000 is looked up again on GeckoTerminal at 6,
24 and 48 hours. If its pools then hold under $50 the liquidity is gone (pulled, or drained by the creator
dumping): the coin is marked, its creator's record counts a pull (`cr_pulled_n`, a factor the model sees),
and that creator is never called again. The dashboard's Learning tab shows the share of coins that lost
their liquidity per launchpad and the creators who did it; the daily summary has one line on it.

**Paper trading (2026-10-17, no money involved).** Every 🔥 high and 🔥🔥 very high conviction call opens a $25 paper
trade (`paper` in config.yaml; `PAPER_TRADING`, `PAPER_STAKE_USD`, `PAPER_MIN_TIER` in Hostinger). It follows
the call's own live price feed and runs the plan: 50% at 2x, 25% at 4x, 25% at 8x, everything at -50% before
the first sale. Kept realistic: bought at the price when the call goes out +3%, every sell 3% below its target
(a stop that is gapped through fills at the price actually seen), 1% fee on each buy and sell; what is still
held when the scanner stops following the coin (6 h at most) is sold at the last price. Each sale is replied
silently under the call, the daily summary has a line, and the dashboard's "Paper trading" tab lists every
trade with its deepest dip before 2x (to judge tighter stops) and its best multiple. A restart closes open
paper trades at their last price.

**Conviction.** Every call is ranked against all snapshots of the last 24 h by whatever decides calls:
🔥🔥 **very high** = top 1%, 🔥 **high** = top 5% (`call.conviction`). On 14-16 Oct the top 1% 2x'd 73%,
4x'd 33% and 10x'd 15% of the time; the top 1-5% 2x'd 47%. Shown on the Telegram call, in the
dashboard's Calls tab and in the daily summary.

## 5. Data sources (verified live on 2026-10-14)

| Source | Used for | Limit seen |
|---|---|---|
| **GMGN OpenAPI** (free key) | New launches on every launchpad (`/v1/trenches`, seconds after creation); per token: holders, top 10, dev, insiders, bundlers, phishing, snipers, bots, fresh wallets, smart money, KOLs, creator history, socials, X followers, TG calls, DexScreener payments, contract safety, LP burn | free plan 5 weight/s (token call 1, feed 2); the scanner needs about 1.5/s |
| Solana RPC websocket, pump.fun program logs | Every pump.fun create / trade / graduation, with slot numbers | 150-350 messages/s; measured 23-35 GB/day of download (0.7-1 TB/month), more when the market is busy |
| GeckoTerminal `new_pools`, `trades` | DEX pools GMGN's feed does not list; first buyers of a pool (for wallet reputation); the only discovery without a GMGN key | ~5 calls/min free (measured), 30 with a free CoinGecko demo key |
| DexScreener `tokens/v1` | Price / volume / txns for 30 tokens per call, any chain; paid profiles | 300/min documented |
| **Codex** (`CODEX_API_KEY`) | Every chain incl. Robinhood: holders, top 10, snipers, bundlers, insiders, suspicious wallets, dev - up to 200 tokens per request | free plan 10,000 requests/month after a one-time $1 activation; the scanner keeps to 300/day |
| **Solana Tracker** (`SOLANATRACKER_API_KEY`) | Solana: snipers, bundlers, insiders, top 10, dev, holders, LP burn, rug flag | free plan 10,000 requests/month; used for call candidates, 300/day |
| GeckoTerminal token info (no key) | Every chain: holder count, mint / freeze authority, honeypot, trust score. Its top-10 share counts the pool or curve as a holder, so it is kept as its own factor only | shares GeckoTerminal's limit: ~5/min free, 30 with a free CoinGecko demo key |
| GoPlus | Second contract check for every token (Solana and EVM; Robinhood Chain = chain 4663) | no key |
| RugCheck | Solana holders, insiders, risk flags when GMGN has no answer | 15/min (was down during testing) |
| Token metadata + t.me | Socials, image, Telegram members | - |

PONS is on Robinhood Chain (GMGN platforms `pons_v2`, `pons`); StonkFun deploys through Raydium
LaunchLab on Solana. Which launchpads are watched per chain: `config.yaml` → `sources.gmgn.chains`;
one name per launchpad across sources: `sources.launchpad_names`.

**Which keys to get.** Each fills what the others miss; none is required.

| Key | Cost | Adds |
|---|---|---|
| `COINGECKO_DEMO_API_KEY` | free (coingecko.com/en/api) | 6x more GeckoTerminal lookups: faster discovery, holder counts for most non-pump snapshots |
| `CODEX_API_KEY` | free plan after a one-time $1 activation (codex.io) | insiders, bundlers, snipers, dev, top 10 on every chain incl. Robinhood |
| `SOLANATRACKER_API_KEY` | free plan (solanatracker.io) | the same for Solana coins, checked before each call |
| `GMGN_API_KEY` | free tier, but keeps a $100 balance in your GMGN account | everything above plus phishing wallets, KOLs, X followers, TG calls, creator history, launch feed |

Codex and Solana Tracker were set up from their documentation; no key was available to test them
live. Codex's docs do not say whether shares come as 0-1 or 0-100, so the scanner detects the
scale from the first answer and logs it.

**Getting the GMGN key (needs a $100 balance in the GMGN account, about 2 minutes):**
1. Make a key pair on this PC (read-only use never needs the private half):
   `openssl genpkey -algorithm ed25519 -out gmgn_private.pem`, then
   `openssl pkey -in gmgn_private.pem -pubout`
2. Sign in at https://gmgn.ai/ai, open the API application form, paste the public key (the
   `-----BEGIN PUBLIC KEY-----` block) and submit. Type the address yourself: look-alike GMGN
   domains exist.
3. Copy the API key it shows into `GMGN_API_KEY` (`.env` on this PC, Docker Manager on the VPS).
4. Keep `gmgn_private.pem` private and out of git; it only matters for trading, which this
   scanner never does.

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

1. Repository [`CryptoLaunchScanner`](https://github.com/sudeep-sasikumar/CryptoLaunchScanner),
   branch `main`. Every push runs the tests, builds the image and publishes
   `ghcr.io/sudeep-sasikumar/cryptolaunchscanner:latest` (GitHub Actions).
2. On GitHub: your profile → Packages → `cryptolaunchscanner` → Package settings → make it
   public, so the VPS can pull it without a login (the same as `ignitionandcoil`). Needed once.
3. Hostinger → VPS → Docker Manager → Compose → Compose from URL:
   `https://raw.githubusercontent.com/sudeep-sasikumar/CryptoLaunchScanner/main/docker-compose.yaml`,
   project name `crypto-launch-scanner`.
4. Enter the environment variables, then Deploy:

   | Variable | Needed | What |
   |---|---|---|
   | `CODEX_API_KEY`, `SOLANATRACKER_API_KEY`, `COINGECKO_DEMO_API_KEY`, `GMGN_API_KEY` | recommended | holder data - see "Which keys to get" above |
   | `TELEGRAM_BOT_TOKEN` | for alerts | from @BotFather; the Ignition & Coil bot works for alerts |
   | `TELEGRAM_CHAT_ID` | for alerts | your chat id; several chats comma-separated; a group id starts with `-100` |
   | `DASHBOARD_PASSWORD` | for the dashboard | a long password of your choice |
   | `TELEGRAM_THREAD_ID` | optional | post into one topic of a forum group |
   | `TELEGRAM_OUTCOME_UPDATES` | optional | `true`/`false`: reply under each call when it hits 2x or stops out (default true) |
   | `TELEGRAM_SILENT_OUTCOMES` | optional | `true`/`false`: those replies arrive without a sound (default true) |
   | `TELEGRAM_DAILY_SUMMARY_UTC` | optional | e.g. `06:00`, or `off` |
   | `TELEGRAM_COMMANDS` | optional | `true` to answer `/status` `/calls` `/stats` - needs a bot of its own (Ignition & Coil already reads its bot's messages) |
   | `CALL_MIN_CHANCE` | optional | only call coins the model gives at least this % chance of 2x (default 50; lower = more calls, higher = fewer) |
   | `CALLS_PER_HOUR` | optional | most calls in one hour (default 10) |
   | `CALL_MIN_TIER` | optional | `1` (default): only 🔥 high and 🔥🔥 very high calls are sent; `2`: very high only; `0`: every call |
   | `PULL_PROOF_ONLY` | optional | `true` (default): only call coins whose liquidity cannot be pulled; `false`: every launchpad |
   | `GMGN_WEIGHT_PER_SEC` | optional | 5 on GMGN's free plan, 20 Plus, 50 Pro |
   | `SOLANA_WS_URL`, `ANTHROPIC_API_KEY`, `DOMAIN` | optional | see `docker-compose.yaml` |

5. Dashboard: `https://srv1479428.hstgr.cloud/radar/` - it shares the hostname and certificate
   that Ignition & Coil already uses. Ignition & Coil keeps everything outside `/radar/`.

Data (database, models, logs) lives in the `radar_data` volume and survives redeploys. A redeploy
restarts the scanner: snapshots still being followed at that moment are set aside, not trained on.

**Network traffic is not disk space.** The pump.fun feed streams every pump.fun transaction through
the VPS: measured 23-35 GB of download a day (0.7-1 TB a month), more in a busy market. Almost all
of it is discarded on arrival; only the compact snapshots are written (see Storage below). That
traffic counts against the VPS plan's monthly bandwidth allowance, not the 20 GB disk cap. Expect a
few hundred MB of RAM.

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
| `call.min_prob` / `call.target_precision` | 0.50 | With a model: minimum chance of 2x to call (`CALL_MIN_CHANCE`) |
| `call.max_per_hour` | 10 | Alert cap |
| `paper.*` | $25, high + very high | Paper trading: stake, tiers, take-profit ladder, stop, slippage, fees |
| `call.conviction` | 5 / 1 | 🔥 high = top 5%, 🔥🔥 very high = top 1% of the last 24 h |
| `call.min_tier` | 1 | Only send 🔥 high and 🔥🔥 very high calls (`CALL_MIN_TIER`: 2 = very high only, 0 = all) |
| `sources.skip_launchpads` | fluxbeam | Never tracked (pool farms with fake runs) |
| `call.pull_proof` | only: true | Calls only for coins on a bonding curve, or graduated on pump.fun / PONS / four.meme (LP burned or locked), or with 95%+ of the LP locked (`PULL_PROOF_ONLY`) |
| `pulls.check_ages_h` | 6 / 24 / 48 | When DEX coins are looked up again to see whether their liquidity is still there |
| `gates.max_creator_pulls` | 1 | A creator whose coin lost its liquidity is never called again |
| `call.mcap_usd` | 4.5k - 400k | Mcap range a call may be in |
| `gates.*` | | Vetoes. Safety ones always apply; the ones marked (*) step aside once a model is in use |
| `learn.auto_retrain` | false | Automatic retraining + promotion (`AUTO_RETRAIN`); off = only `tools\retrain.py` |
| `learn.retrain_hours` | 12 | Retrain cadence when automatic |
| `storage.max_gb` | 20 | Disk cap for database + models |

Override without editing the file: `RADAR_OVERRIDES="call.top_pct=1;label.target=3"`.

## 9. Known limits

- **First day = scorecard only.** The weights were rebalanced on just 3 days of data: a tested lead, not a proven edge.
- **Launchpads other than pump.fun need at least one holder-data key.** Measured with no keys
  (7-minute live run): contract safety was checked on every snapshot, but holder figures existed
  for only 4 of 19 non-pump snapshots, because GeckoTerminal's free limit is spent on discovery
  and RugCheck fails on most brand-new tokens. A free CoinGecko demo key, Codex or Solana Tracker
  closes most of that gap. GMGN's public demo key is shared by everyone and was rate-limited
  almost constantly, so it is never used.
- **Phishing-wallet shares come only from GMGN** (and pump.fun's own count of tokens sold by
  wallets that never bought). Without GMGN that factor scores neutral outside pump.fun.
- **GMGN's figures are GMGN's.** Its insider, bundler and phishing labels are its own wallet
  classification; they are stored under `gm_*` names so the learner can test how much they matter.
- **Off-pump.fun coins are followed by 15-second price snapshots**, not trade by trade; a dev's
  exact share sold is visible only on pump.fun (elsewhere: holding / closed from GMGN).
- **RugCheck can be unavailable** (it returned errors for every token during testing).
- **No live X mention counts** (paid API). X followers and Telegram call counts come from GMGN.
- **The public Solana RPC can drop.** It dropped 3 times in 7 minutes on 2026-10-15. Tokens live
  during a drop are set aside and never trained on, so drops cost data. A private RPC websocket in
  `SOLANA_WS_URL` (for example a free Helius key) should be steadier; not tested here.
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
radar/paper.py           paper trading: the take-profit plan on live prices, no money
radar/sources/           pump.fun stream + decoder, GMGN, Codex / Solana Tracker / GeckoTerminal info (intel.py),
                         GeckoTerminal pools, DexScreener, GoPlus, RugCheck, metadata
radar/llm.py             optional Claude meme / narrative rating
radar/notify.py          Telegram (alerts, outcome replies, daily summary, optional commands)
radar/web/               dashboard
radar/export.py          daily research files served at /api/research
tools/sync_research.py   PC: daily copy of the research files + traits report
tools/analyze.py         PC: what the 2x / 4x / 10x / 100x runners have in common
tools/retrain.py         PC: ask the VPS for a retrain (automatic retraining is off)
tests/                   72 tests
```
