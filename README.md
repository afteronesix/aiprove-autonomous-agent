# best-autonomous-agent — AIProve entry

A fully autonomous AIProve agent: no human in the loop. It reads
`skill.md`, signs in with its own keypair, and enters + completes as
many open bounties as it can on its own.

## Run

```sh
pip install -r requirements.txt
python agent.py            # forecasts + prediction-shaped bounties
GH_TOKEN=... GH_USER=... python agent.py   # + repo-shaped code bounties
```

No funds needed — entering and submitting is free. Re-runs are safe:
bounties already submitted are skipped (checked via
`/api/me/submissions`).

## What it completes, and how

| Bounty | How the agent decides |
|---|---|
| `btc-tomorrow`, `forecast-eth-24h`, `forecast-btc-7d` | CoinGecko 24h change → momentum: down ≥0.5% ⇒ "no", up ≥0.5% ⇒ "yes", confidence `min(0.65, 0.52 + |chg|·0.02)` |
| `signal-eth-pnl-24h` | ETH 24h change < 0 ⇒ short, else long, same confidence rule |
| `spy-close-1dollar`, `forecast-gold-tomorrow`, `forecast-nvda-3d` | Yahoo Finance last price ⇒ persistence forecast (rounded to whole $) |
| `top5-pons-24h` | Newest Pons launches from the free loopholetape feed; symbols resolved from each token's server-rendered Pons page |
| `bug-hunt-utils` | Submits its bundled, execution-verified bug reports (`solutions/bug-hunt-utils/bugs.json`) |
| `fastest-primes`, `code-golf-expr` | Creates a GitHub repo via the REST API and pushes its bundled solution — only when `GH_TOKEN` + `GH_USER` are set |
| PosterJudge content, `eth-pnl-fixed`, `refer-most-agents` | Skipped with a logged reason (need an LLM, strategy research, or referrals) |

Identity: the agent generates its own keypair (`agent_key.json`,
chmod 600, gitignored) and authenticates with EIP-191 `personal_sign`
(challenge → sign → verify → Bearer token), exactly as `skill.md`
specifies.

## Tested end-to-end (live API)

Ran `python agent.py` against the live board with a fresh throwaway
keypair: **9 bounties completed, 0 failed** — all 7 forecast/trading
predictions (BTC no/0.60, ETH no/0.59, ETH short/0.59, SPY 750, GLD
356, NVDA 160), `top5-pons-24h` (ZINLEY, OSSOFF, PERPS, GTAVI, KLIQO),
and `bug-hunt-utils` (5 reports). A second run correctly skipped all 9
as already submitted. The GitHub path was tested with an invalid token
and fails gracefully (`repo create HTTP 401` → skip, exit 0). The
throwaway test keypair was deleted afterwards.
