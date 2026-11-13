#!/usr/bin/env python3
"""Fully autonomous AIProve agent — no human in the loop.

Reads https://aiprove.fun/skill.md, signs in with its OWN keypair, and
enters + completes as many open bounties as it can on its own:

- Forecast/trading bounties: decides from LIVE prices (CoinGecko for
  crypto, Yahoo Finance chart API for stocks/ETFs) with documented,
  deterministic rules (momentum for direction, persistence for closes).
- top5-pons-24h: takes the newest Pons launches from the free
  loopholetape feed, resolves symbols from their public record pages.
- bug-hunt-utils: submits its bundled, verified bug reports.
- Repo-shaped code bounties (fastest-primes, code-golf-expr): creates a
  GitHub repo and pushes its bundled solution — ONLY when GH_TOKEN and
  GH_USER are set; otherwise skipped with a logged reason.
- PosterJudge content, eth-pnl-fixed, refer-most-agents: skipped with a
  logged reason (they need an LLM, strategy research, or referrals).

Already-submitted bounties are skipped (checked via /api/me/submissions),
so re-runs are safe. Every step is logged; a summary JSON is printed.

Setup:
    pip install -r requirements.txt
    python agent.py            # forecasts + prediction-shaped bounties
    GH_TOKEN=... GH_USER=... python agent.py   # + repo-shaped code bounties

No funds needed: entering and submitting is free.
"""
import base64
import json
import os
import re
import sys

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

API_BASE = "https://aiprove.fun"
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "agent_key.json")
SOLUTIONS = os.path.join(HERE, "solutions")
HANDLE = "autonomous.agent"
UA = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20

# slug -> (coingecko id or yahoo symbol, kind)
BINARY = {  # yes/no "will it close higher", Brier-scored
    "btc-tomorrow": ("bitcoin", "cg"),
    "forecast-eth-24h": ("ethereum", "cg"),
    "forecast-btc-7d": ("bitcoin", "cg"),
}
POINT = {  # predict the close (whole dollars), absolute-error scored
    "spy-close-1dollar": "SPY",
    "forecast-gold-tomorrow": "GLD",
    "forecast-nvda-3d": "NVDA",
}
REPO_CODE = {  # slug -> bundled solution dir + files to push
    "fastest-primes": ("fastest-primes", ["primes.c", "run.sh"]),
    "code-golf-expr": ("code-golf-expr", ["expr.js"]),
}


def log(msg):
    print(msg, flush=True)


def cg_change(asset):
    """24h % change for a CoinGecko id, or None on failure."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": asset, "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            headers=UA, timeout=TIMEOUT)
        return float(r.json()[asset]["usd_24h_change"])
    except Exception as e:
        log(f"  CoinGecko {asset} failed: {e}")
        return None


def yahoo_price(symbol):
    """Last price for a Yahoo symbol, or None on failure."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "5d", "interval": "1d"},
            headers=UA, timeout=TIMEOUT)
        meta = r.json()["chart"]["result"][0]["meta"]
        return float(meta.get("regularMarketPrice")
                     or meta["chartPreviousClose"])
    except Exception as e:
        log(f"  Yahoo {symbol} failed: {e}")
        return None


def momentum_prediction(chg):
    """Direction call from a 24h % change; confidence capped at 0.65."""
    conf = round(min(0.65, 0.52 + abs(chg) * 0.02), 2)
    if chg <= -0.5:
        return {"answer": "no", "confidence": conf}
    if chg >= 0.5:
        return {"answer": "yes", "confidence": conf}
    return {"answer": "no" if chg < 0 else "yes", "confidence": 0.52}


def pons_top5():
    """Five newest Pons launch symbols via the free loopholetape feed.

    The feed gives token addresses, not symbols; each token's Pons
    page (ponsfamily.com/launchpad/<address>) is server-rendered with
    "<title>Name (SYMBOL) | Pons". Returns a list of 5 symbols, or
    None if fewer than 5 resolve.
    """
    try:
        items = requests.get(
            "https://api.loopholetape.com/v1/rhc/sample/launches",
            headers=UA, timeout=TIMEOUT).json()["data"]["items"]
    except Exception as e:
        log(f"  loopholetape feed failed: {e}")
        return None
    symbols = []
    for it in items[:5]:
        token = it.get("token")
        if not token:
            continue
        try:
            html = requests.get(
                f"https://ponsfamily.com/launchpad/{token}",
                headers=UA, timeout=TIMEOUT).text
            m = re.search(r"<title>[^<]*\(([A-Za-z0-9]{1,20})\)\s*\| Pons",
                          html)
            if m:
                symbols.append(m.group(1))
        except Exception:
            continue
    return symbols if len(symbols) >= 5 else None


class Agent:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(UA)
        self.done, self.skipped, self.failed = [], [], []

    # ---- identity + auth (same flow as the sample agent) ----
    def keypair(self):
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE) as f:
                return Account.from_key(json.load(f)["private_key"])
        acct = Account.create()
        with open(KEY_FILE, "w") as f:
            json.dump({"address": acct.address,
                       "private_key": acct.key.hex()}, f)
        os.chmod(KEY_FILE, 0o600)
        log(f"generated fresh keypair: {acct.address}")
        return acct

    def auth(self, acct):
        ch = self.session.get(
            f"{API_BASE}/api/auth/challenge",
            params={"address": acct.address}, timeout=TIMEOUT).json()
        sig = acct.sign_message(
            encode_defunct(text=ch["challenge"])).signature.hex()
        v = self.session.post(
            f"{API_BASE}/api/auth/verify",
            json={"address": acct.address, "signature": "0x" + sig},
            timeout=TIMEOUT).json()
        self.session.headers["Authorization"] = f"Bearer {v['token']}"
        log(f"authenticated as {acct.address}")

    # ---- bounty helpers ----
    def open_bounties(self):
        d = self.session.get(f"{API_BASE}/api/bounties",
                             params={"status": "open"},
                             timeout=TIMEOUT).json()
        return d.get("bounties") or []

    def already_submitted(self):
        d = self.session.get(f"{API_BASE}/api/me/submissions",
                             timeout=TIMEOUT).json()
        subs = d.get("submissions") or []
        return {s.get("bountySlug") for s in subs}

    def enter(self, slug):
        r = self.session.post(f"{API_BASE}/api/bounties/{slug}/enter",
                              json={"handle": HANDLE}, timeout=TIMEOUT)
        return r.status_code in (200, 201)

    def submit_prediction(self, slug, prediction):
        r = self.session.post(
            f"{API_BASE}/api/bounties/{slug}/submit",
            json={"prediction": prediction, "handle": HANDLE},
            timeout=TIMEOUT)
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:200]}
        ok = r.status_code in (200, 201) and body.get("status") in (
            "provenance_ok", "ok", "submitted")
        return ok, body

    def submit_repo(self, slug, repo_url, commit):
        r = self.session.post(
            f"{API_BASE}/api/bounties/{slug}/submit",
            json={"repoUrl": repo_url, "commit": commit,
                  "transcript": f"Autonomous agent entry for {slug}: "
                  "bundled deterministic solution, pushed by the agent "
                  "itself via the GitHub API.",
                  "handle": HANDLE},
            timeout=TIMEOUT)
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:200]}
        ok = r.status_code in (200, 201) and body.get("status") in (
            "provenance_ok", "ok", "submitted")
        return ok, body

    # ---- GitHub via REST API (no gh CLI needed in the sandbox) ----
    def gh_push_solution(self, slug):
        token = os.environ.get("GH_TOKEN")
        user = os.environ.get("GH_USER")
        if not token or not user:
            return None, "GH_TOKEN/GH_USER not set"
        subdir, files = REPO_CODE[slug]
        repo = f"aiprove-{slug}"
        gh = {"Authorization": f"Bearer {token}",
              "Accept": "application/vnd.github+json",
              "User-Agent": "Mozilla/5.0"}
        base = "https://api.github.com"
        r = requests.post(f"{base}/user/repos",
                          json={"name": repo, "public": True,
                                "auto_init": False},
                          headers=gh, timeout=TIMEOUT)
        if r.status_code not in (201, 422):  # 422 = already exists
            return None, f"repo create HTTP {r.status_code}"
        sha = None
        for fn in files:
            path = os.path.join(SOLUTIONS, subdir, fn)
            with open(path, "rb") as f:
                content = base64.b64encode(f.read()).decode()
            url = f"{base}/repos/{user}/{repo}/contents/{fn}"
            payload = {"message": f"autonomous agent: {slug} ({fn})",
                       "content": content, "branch": "main"}
            cur = requests.get(url, headers=gh, timeout=TIMEOUT)
            if cur.status_code == 200:
                payload["sha"] = cur.json()["sha"]
            put = requests.put(url, json=payload, headers=gh,
                               timeout=TIMEOUT)
            if put.status_code not in (200, 201):
                return None, f"push {fn} HTTP {put.status_code}"
            sha = put.json()["commit"]["sha"]
        return (f"https://github.com/{user}/{repo}", sha), None

    # ---- per-bounty dispatch ----
    def attempt(self, bounty):
        slug = bounty["slug"]
        if slug in BINARY:
            asset, _ = BINARY[slug]
            chg = cg_change(asset)
            if chg is None:
                return self.skip(slug, "price feed unavailable")
            pred = momentum_prediction(chg)
            log(f"  {asset} 24h {chg:+.2f}% -> {pred}")
            return self.complete_prediction(slug, pred)
        if slug == "signal-eth-pnl-24h":
            chg = cg_change("ethereum")
            if chg is None:
                return self.skip(slug, "price feed unavailable")
            pred = {"answer": "short" if chg < 0 else "long",
                    "confidence": round(min(0.65, 0.52 + abs(chg) * 0.02),
                                        2)}
            log(f"  ETH 24h {chg:+.2f}% -> {pred}")
            return self.complete_prediction(slug, pred)
        if slug in POINT:
            price = yahoo_price(POINT[slug])
            if price is None:
                return self.skip(slug, "price feed unavailable")
            pred = {"answer": round(price)}
            log(f"  {POINT[slug]} last ${price:.2f} -> {pred} "
                "(persistence)")
            return self.complete_prediction(slug, pred)
        if slug == "top5-pons-24h":
            syms = pons_top5()
            if not syms:
                return self.skip(slug, "could not resolve 5 symbols")
            log(f"  pons newest 5: {syms}")
            return self.complete_prediction(slug, {"answer": syms})
        if slug == "bug-hunt-utils":
            with open(os.path.join(SOLUTIONS, "bug-hunt-utils",
                                   "bugs.json")) as f:
                reports = json.load(f)
            return self.complete_prediction(slug, reports)
        if slug in REPO_CODE:
            (res, err) = self.gh_push_solution(slug)
            if err:
                return self.skip(slug, err)
            repo_url, commit = res
            ok, body = self.submit_repo(slug, repo_url, commit)
            return self.record(slug, ok, body)
        if bounty.get("lane") == "PosterJudge":
            return self.skip(slug, "needs LLM/image generation")
        return self.skip(slug, "no autonomous handler")

    def complete_prediction(self, slug, pred):
        self.enter(slug)
        ok, body = self.submit_prediction(slug, pred)
        return self.record(slug, ok, body)

    def record(self, slug, ok, body):
        if ok:
            self.done.append(slug)
            log(f"  COMPLETED {slug} (id={body.get('id')})")
        else:
            self.failed.append(slug)
            log(f"  FAILED {slug}: {str(body)[:200]}")
        return ok

    def skip(self, slug, reason):
        self.skipped.append((slug, reason))
        log(f"  skipped {slug}: {reason}")
        return False

    def run(self):
        skill = self.session.get(f"{API_BASE}/skill.md",
                                 timeout=TIMEOUT).text
        log(f"skill.md read ({len(skill)} chars)")
        acct = self.keypair()
        self.auth(acct)
        bounties = self.open_bounties()
        log(f"open bounties: {len(bounties)}")
        submitted = self.already_submitted()
        log(f"already submitted: {sorted(submitted) or 'none'}")
        for b in bounties:
            slug = b["slug"]
            if slug in submitted:
                log(f"  skipped {slug}: already submitted")
                self.skipped.append((slug, "already submitted"))
                continue
            log(f"attempting {slug} [{b.get('lane')}, "
                f"{b.get('metric')}]")
            try:
                self.attempt(b)
            except Exception as e:
                self.failed.append(slug)
                log(f"  FAILED {slug}: {e!r}")
        summary = {"completed": self.done,
                   "failed": self.failed,
                   "skipped": self.skipped}
        log("\nSUMMARY: " + json.dumps(summary, indent=2))
        return 0 if not self.failed else 1


if __name__ == "__main__":
    raise SystemExit(Agent().run())
