# tgrate — real exchange result through Telegram Wallet

🇷🇺 [Русская версия](README.ru.md)

A web panel and a Telegram userbot that calculate how much you will actually
receive when moving money through Telegram's crypto Wallet: the P2P Market,
the internal exchange and withdrawals to a blockchain. It finds the best route
between two points (e.g. rubles on a card → USDT in the Wallet → USDT on TON),
applies every fee on every step and shows which reliable sellers can fill the
deal right now.

**Stack:** Python 3.12, FastAPI + uvicorn, Telethon, httpx, SQLite, pytest.

---

## What it does

- **Best route** between banks, Wallet balances and networks (RUB, KZT, UAH,
  USD, EUR, TRY; USDT, GRAM, BTC, USDC in the Wallet; USDT on TON / TRON /
  Solana / Ethereum, GRAM, BTC on-chain; transfer to a Telegram contact), with
  the fee and result of each step.
- **P2P price for your amount**, not the top line of the order book (see
  "Where the accuracy comes from").
- **Top-3 reliable sellers/buyers** for every P2P step: name, price, limits,
  completion rate, number of deals, payment time.
- **Payment method choice** (SBP, bank cards, SEPA, Revolut, Wise, …) — the list
  comes from the live order book.
- **Tracked routes:** an exact result for a fixed amount once a minute
  (`TRACK_ROUTES`), drawn as a chart in the panel.
- **Calibration by real deals:** enter what you actually received, and the
  forecast for that direction adjusts.
- **Userbot** in your own Telegram account: the same calculation as a command
  in Saved Messages or any chat.

## Where the accuracy comes from

Each of the three parts is worth about a percent on its own.

**Order book depth for your amount.** The P2P price is taken from the best
reliable maker who can fill the whole amount in one deal — respecting the ad's
`minAmount` / `maxAmount` and available volume. If one deal cannot cover it,
a weighted average over several reliable makers is used, each piece within the
ad's limits. The top line of the book is often not executable ("91 ₽, but only
exactly 100,000 ₽"); ignoring limits gave errors of up to 8%.

**Counterparty filter.** Reliable means the Wallet "Reliable seller" status or
at least 95% completion and 250 deals (the status requirements). Makers below
`WALLET_MIN_EXECUTE_RATE` are excluded entirely.

**Calibration by fact.** After a deal the received amount is recorded (in the
panel, via `/api/deals`, or `.факт` in the userbot). For each route and pair
the system keeps the ratio actual / predicted and applies a correction:
weighted by recency (21-day half-life), averaged with the median to resist
outliers, ratios outside 0.5–2 dropped, at least 3 deals required, capped at
±8%. The ratio is measured against the model without correction, so the
correction does not oscillate. Until there are deals, the panel says so and
the number rests on the fee model alone.

## Fees

Checked against help.ru.wallet.tg and docs.wallet.tg on 27.09.2026. Wallet
changes tariffs, so all numbers live in `app/wallet.py` and `fees.yaml`.

| Operation | Fee |
|---|---|
| P2P Market | maker only: RUB 2%, KZT/UAH/USD/EUR/TRY 1.2% (Reliable seller: 1.6% / 0.9%); taker pays 0 |
| Internal exchange | 0.9% on Basic, 0.09% on Trader |
| USDT withdrawal: TON / TRON / Solana / Ethereum | 1 / 3.5 / 1.5 / 4 USDT, fixed |
| GRAM (former TON) withdrawal | 0.05 GRAM |
| BTC withdrawal | 0.00008 BTC, floating |
| Deposit, transfer to a Telegram contact | 0 |
| Selling crypto to a card via a partner | 3.5% — not in the route graph, the provider's rate is not visible |

What follows from the table:

1. **Maker or taker is the biggest lever.** Answering someone else's ad costs
   nothing; posting your own costs 1.2–2%. Set `WALLET_P2P_ROLE` honestly.
2. **Fixed withdrawal fees kill small amounts.** 3.5 USDT on TRON is 3.5% of
   $100 and 0.07% of $5,000, so on small amounts the route almost always goes
   through TON.
3. **The Trader tier makes exchange ten times cheaper** (0.9% → 0.09%);
   threshold — $50,000 monthly volume.

Exchange limits (USD equivalent): 1.4–25,000 per pair, BTC pairs from 3.1,
USDT–BTC up to 100,000, USDT–SOL up to 50,000, USDT–ETH up to 100,000.
Withdrawal minimums: 1 USDT, 0.0001 BTC, 0.1 GRAM.

## How the calculation works

1. **Quotes.** P2P prices come from the official Wallet P2P API
   (`POST p2p.walletbot.me/p2p/integration-api/v1/item/online`, read-only key
   from Wallet → P2P Market → Profile → API Keys; data refreshes every 30 s).
   Internal exchange rates use Binance spot with Bybit as a fallback. Binance
   P2P is fetched only for comparison in `.статус` and does not affect routes.
2. **Graph.** `fees.yaml` describes nodes (where the money is) and edges
   (operations with their fees and limits). Rates are bound to the edges for
   the requested amount and payment method.
3. **Search.** A best-first search maximises the output amount (Dijkstra-like:
   every operation is monotonic in its input — more in, not less out), up to
   6 hops, no cycles, returning the k best routes.
4. **Correction.** The calibration factor for the route is applied, and the
   response contains each step, the fees, the sellers and the correction
   basis.

## Running

```bash
make install     # .venv, dependencies, .env from the template
# put WALLET_API_KEY (and the session data for the userbot) into .env
make check       # API key, live order book, auto-check of the `side` semantics
make test        # fee engine tests, no network needed
make run         # panel on 127.0.0.1:8080
make bot         # userbot as a separate process
```

`make session` turns an existing Telethon `.session` file into a
`StringSession` without logging in again.

`make check` also resolves the one ambiguity in the Wallet docs: they do not
say whose side the `side` field of an ad describes, and that decides which
price counts as the buy price. The script compares the average prices of both
sides (sellers' must be higher) and tells you what to put into
`WALLET_SIDE_WHEN_BUYING`. Do not run the panel until the check passes: a
mistake here flips the spread.

`docker compose up -d` runs the panel and the userbot as two services if you
prefer Docker.

### Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `WALLET_API_KEY` | Wallet P2P API key (read-only) |
| `WALLET_TIER` | `basic` (exchange 0.9%) or `trader` (0.09%) |
| `WALLET_P2P_ROLE` | `taker` (0%) or `maker` (1.2–2%) |
| `WALLET_SIDE_WHEN_BUYING` | ad side we buy from — `make check` tells the value |
| `WALLET_MIN_EXECUTE_RATE`, `WALLET_MERCHANTS_ONLY` | counterparty filters |
| `WALLET_PAYMENTS` | default payment methods |
| `SAFETY_PCT` | extra margin subtracted from the result |
| `TRACK_ROUTES` | routes for the per-minute chart, e.g. `USDT.WALLET>RUB.BANK:1000` |
| `HOST`, `PORT`, `PANEL_TOKEN` | panel address; a non-empty token enables auth |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION`, `TG_ALLOWED_USERS`, `API_BASE` | userbot |

## Userbot

Runs on an existing session of your own account and calls the panel's API.

```
.к 50000 RUB.BANK USDT.WALLET   — calculate a route
.узлы                           — list of nodes
.статус                         — sources, accuracy, Wallet vs Binance P2P
.факт <route> <in> <forecast> <received> — record a real deal
.помощь                         — help
.raw                            — show the raw text of a replied message
```

It also recognises completed-deal notifications from @wallet and logs them;
automatic recording into calibration is deliberately off until the parser is
checked against real notifications (`.raw` helps with that).

## Deploying to a VPS

```bash
sudo deploy/install.sh     # copies to /opt/tgrate, creates .venv with uv, installs systemd units
make logs                  # journalctl -u tgrate-api -u tgrate-bot
```

The installer copies the project to `/opt/tgrate` (excluding `.venv`, `.git`,
`data`), creates `.env` from the template on the first run and stops so you
can fill it in, then enables `tgrate-api` and — if `TG_SESSION` is set —
`tgrate-bot`. Re-run it after changes in the source directory: `/opt/tgrate`
is a copy, not a link.

Expose the panel only through `deploy/nginx.conf` (TLS + basic auth) and with
a non-empty `PANEL_TOKEN`: the userbot has full access to the account, an open
panel does not deserve that.

## Structure

```
fees.yaml               operation graph and all fees
app/wallet.py           Wallet P2P API client, tariffs, depth fill, top sellers
app/quotes.py           quote aggregator (Wallet, spot, Binance P2P), depth for an amount
app/providers/          spot (Binance + Bybit) and Binance P2P sources
app/graph.py            route search
app/models.py           applying fees to an amount, routes and steps
app/calibration.py      corrections from real deals (SQLite)
app/history.py          quote snapshots and tracked routes (SQLite)
app/config.py           settings from .env
app/main.py             panel API (FastAPI)
app/userbot.py          Telethon userbot
static/index.html       panel
scripts/check.py        pre-launch self-check
scripts/session.py      StringSession from an existing session
tests/                  fee engine tests
deploy/                 systemd units, nginx, installer
```
