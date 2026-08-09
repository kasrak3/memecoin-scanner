#!/usr/bin/env python3
"""
Memecoin safety pre-filter for new Solana tokens (pump.fun / DexScreener).

What this does:
  - Pulls freshly-created Solana tokens from RugCheck's public API.
  - Waits until each one has real trading activity on DexScreener.
  - Runs rug-risk checks: mint/freeze authority, holder concentration,
    holder count, liquidity, volume, insider/bundled-wallet clustering,
    and bonding-curve vs. graduated-pool stage.
  - Sends anything that passes to a Telegram chat via a bot.

What this does NOT do:
  - It does not predict price, does not buy or sell anything, and does not
    guarantee a token is safe. It only screens out some of the most common,
    mechanically-detectable rug patterns (fake-renounce, insider wallet
    clusters, near-zero liquidity, one wallet holding most of supply).
    Roughly 98%+ of pump.fun launches never gain real traction at all, and
    passing this filter does not change that base rate — it just keeps you
    out of the most mechanically obvious traps. Most tokens that pass this
    filter will still lose value. This is not financial advice.

State (which tokens have already been checked/alerted) is persisted to
scanner_state.json next to this script, so repeated runs don't double-alert.

Delivery: Telegram, because Telegram's Bot API accepts a plain GET request
for sendMessage, which works from restricted/sandboxed environments that
can't make outbound POST requests (Discord webhooks require POST and were
not usable in the environment this was originally built in).
"""

import html
import json
import os
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# ---------- CONFIG (tune these) ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "scanner_state.json")

MIN_AGE_MINUTES = 10           # skip brand-new mints with no trading yet
MIN_LIQUIDITY_USD = 3000       # pool liquidity floor
MIN_VOLUME_H1_USD = 1000       # last-hour volume floor (proof of real activity)
MAX_TOP_HOLDER_PCT = 15.0      # largest non-pool holder, % of supply
MIN_HOLDERS = 30               # distinct holder count
MAX_INSIDER_CLUSTERS = 0       # RugCheck-detected insider/bundle networks allowed
MAX_PENDING_HOURS = 2          # stop re-checking a candidate after this long
MAX_CHECKS_PER_RUN = 500       # cap DexScreener/RugCheck calls per run (raised so newer candidates aren't starved behind stuck old ones)

RUGCHECK_NEW = "https://api.rugcheck.xyz/v1/stats/new_tokens"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"

HEADERS = {"User-Agent": "Mozilla/5.0 (memecoin-safety-scanner)"}

DISCLAIMER = ("Automated safety pre-filter only — not financial advice and not a buy "
              "signal. It only rules out some obvious rug patterns; most tokens that "
              "pass this filter still lose value.")


# ---------- HTTP / TIME HELPERS ----------
def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_ISO_RE = re.compile(r"^(?P<base>.*?)(?P<frac>\.\d+)?(?P<tz>[+-]\d{2}:\d{2})?$")


def parse_iso(ts):
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    m = _ISO_RE.match(ts)
    base, frac, tz = m.group("base"), m.group("frac"), m.group("tz")
    frac = ("." + frac[1:7].ljust(6, "0")) if frac else ""
    tz = tz or "+00:00"
    return datetime.fromisoformat(f"{base}{frac}{tz}")


# ---------- STATE ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"pending": {}, "alerted": [], "rejected": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------- TELEGRAM ----------
def fmt_usd(n):
    n = n or 0
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:,.0f}"


def risk_score_10(score):
    """Rescale RugCheck's raw score (roughly 0-100+, lower=safer) to a
    simple 1 (safest) - 10 (riskiest) scale for the alert."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "n/a"
    return max(1, min(10, round(s / 10)))


def format_alert_text(token):
    symbol = html.escape(str(token.get("symbol") or "?"))
    mint = html.escape(token["mint"])
    lines = [
        f"🟢 <b>{symbol}</b> passed the safety pre-filter",
        "",
        f"🏷 <b>Market cap:</b> {fmt_usd(token.get('market_cap'))}",
        f"💧 <b>Liquidity:</b> {fmt_usd(token['liquidity'])}",
        f"📊 <b>Volume (1h):</b> {fmt_usd(token['volume_h1'])}",
        f"⏱ <b>Age:</b> {token['age_minutes']:.0f} min · <b>Stage:</b> {token['stage']}",
        "",
        f"👥 <b>Holders:</b> {token['holders']}   🐋 <b>Top holder:</b> {token['top_holder_pct']:.1f}%",
        f"🔒 <b>Mint/Freeze authority:</b> Renounced",
        f"🕵️ <b>Insider clusters:</b> {token['insider_clusters']}",
        f"🛡 <b>RugCheck score</b> (1=safest, 10=riskiest): {risk_score_10(token['risk_score'])}/10",
        "",
        f"<code>{mint}</code>",
        "",
        f'🔗 <a href="{token["dexscreener_url"]}">DexScreener</a> · '
        f'<a href="{token["rugcheck_url"]}">RugCheck</a> · '
        f'<a href="https://pump.fun/{token["mint"]}">Pump.fun</a>',
        "",
        f"<i>{html.escape(DISCLAIMER)}</i>",
    ]
    return "\n".join(lines)


def send_telegram_alert(token):
    text = format_alert_text(token)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[no telegram bot configured] Would have alerted:")
        print(text)
        return
    url = TELEGRAM_SEND.format(token=TELEGRAM_BOT_TOKEN)
    params = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    req = urllib.request.Request(f"{url}?{params}", headers=HEADERS, method="GET")
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"Alerted: {token['symbol']} ({token['mint']})")
    except urllib.error.HTTPError as e:
        print(f"Telegram send error: {e.code} {e.read()}")


# ---------- CORE LOGIC ----------
def discover_candidates(state):
    try:
        new_tokens = http_get_json(RUGCHECK_NEW)
    except Exception as e:
        print(f"Failed to fetch new_tokens: {e}")
        return
    now = datetime.now(timezone.utc)
    for t in new_tokens:
        mint = t.get("mint")
        if not mint:
            continue
        if mint in state["pending"] or mint in state["alerted"] or mint in state["rejected"]:
            continue
        state["pending"][mint] = {
            "symbol": t.get("symbol", "?"),
            "created_at": t.get("createAt"),
            "first_seen": now.isoformat(),
        }


def evaluate_pending(state):
    now = datetime.now(timezone.utc)
    to_drop = []
    checks_done = 0

    # Check oldest pending candidates first so nothing starves.
    ordered = sorted(state["pending"].items(), key=lambda kv: kv[1]["first_seen"])

    for mint, info in ordered:
        if checks_done >= MAX_CHECKS_PER_RUN:
            break

        created_dt = parse_iso(info.get("created_at")) or now
        age_minutes = (now - created_dt).total_seconds() / 60

        first_seen_dt = parse_iso(info["first_seen"]) or now
        pending_hours = (now - first_seen_dt).total_seconds() / 3600
        if pending_hours > MAX_PENDING_HOURS:
            print(f"Dropping stale candidate {info['symbol']} ({mint}): "
                  f"pending {pending_hours:.1f}h with no successful evaluation")
            to_drop.append(mint)
            continue

        if age_minutes < MIN_AGE_MINUTES:
            continue  # too fresh, check again next run

        checks_done += 1
        try:
            dex = http_get_json(DEXSCREENER_TOKEN.format(mint=mint))
        except Exception as e:
            print(f"DexScreener fetch failed for {info['symbol']} ({mint}): {e}")
            continue
        pairs = dex.get("pairs") or []
        if not pairs:
            continue  # no market yet, check again next run

        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
        volume_h1 = (pair.get("volume") or {}).get("h1", 0) or 0
        market_cap = pair.get("marketCap") or pair.get("fdv") or 0

        if liquidity < MIN_LIQUIDITY_USD or volume_h1 < MIN_VOLUME_H1_USD:
            continue  # not enough real activity yet, check again next run

        try:
            report = http_get_json(RUGCHECK_REPORT.format(mint=mint))
        except Exception as e:
            print(f"RugCheck fetch failed for {info['symbol']} ({mint}): {e}")
            continue

        mint_auth = (report.get("token") or {}).get("mintAuthority")
        freeze_auth = (report.get("token") or {}).get("freezeAuthority")
        top_holders = report.get("topHolders", [])
        total_holders = report.get("totalHolders", 0)
        known = report.get("knownAccounts", {})

        real_holders = [
            h for h in top_holders
            if known.get(h.get("owner", ""), {}).get("type") not in ("AMM", "LOCKER")
        ]
        top_holder_pct = real_holders[0]["pct"] if real_holders else 0

        # Insider / bundled-wallet clustering: RugCheck flags groups of
        # top holders that were funded from the same source (classic
        # "fake decentralization" pattern — team seeds many wallets that
        # look independent but move together).
        insider_clusters = report.get("graphInsidersDetected", 0) or 0
        insider_networks = report.get("insiderNetworks") or []
        if insider_networks:
            insider_clusters = max(insider_clusters, len(insider_networks))

        # Bonding-curve (still on pump.fun) vs. graduated pool (PumpSwap/
        # Raydium, where LP is auto-burned on graduation). Graduated pools
        # have already survived the highest-risk window; bonding-curve
        # tokens can still have their creator allocation dumped at any time.
        markets = report.get("markets") or []
        market_types = {m.get("marketType") for m in markets}
        if "pump_fun" in market_types and len(market_types) == 1:
            stage = "bonding curve (pre-graduation)"
        elif market_types:
            stage = "graduated pool (LP burned)"
        else:
            stage = "unknown"

        risk_score = report.get("score_normalised", report.get("score"))

        reasons = []
        if mint_auth:
            reasons.append("mint authority not renounced")
        if freeze_auth:
            reasons.append("freeze authority not renounced")
        if top_holder_pct > MAX_TOP_HOLDER_PCT:
            reasons.append(f"top holder {top_holder_pct:.1f}% > {MAX_TOP_HOLDER_PCT}%")
        if total_holders < MIN_HOLDERS:
            reasons.append(f"only {total_holders} holders")
        if insider_clusters > MAX_INSIDER_CLUSTERS:
            reasons.append(f"{insider_clusters} insider/bundled wallet cluster(s) detected")

        if reasons:
            print(f"Rejected {info['symbol']} ({mint}): {', '.join(reasons)}")
            state["rejected"].append(mint)
            to_drop.append(mint)
            continue

        token = {
            "mint": mint,
            "symbol": pair["baseToken"]["symbol"],
            "liquidity": liquidity,
            "volume_h1": volume_h1,
            "market_cap": market_cap,
            "age_minutes": age_minutes,
            "top_holder_pct": top_holder_pct,
            "holders": total_holders,
            "insider_clusters": insider_clusters,
            "stage": stage,
            "risk_score": risk_score,
            "dexscreener_url": pair.get("url", f"https://dexscreener.com/solana/{mint}"),
            "rugcheck_url": f"https://rugcheck.xyz/tokens/{mint}",
        }
        send_telegram_alert(token)
        state["alerted"].append(mint)
        to_drop.append(mint)

    for mint in to_drop:
        state["pending"].pop(mint, None)

    state["rejected"] = state["rejected"][-2000:]
    state["alerted"] = state["alerted"][-2000:]


def main():
    state = load_state()
    discover_candidates(state)
    evaluate_pending(state)
    save_state(state)
    print(f"Done. Pending: {len(state['pending'])}, "
          f"Alerted total: {len(state['alerted'])}, "
          f"Rejected total: {len(state['rejected'])}")


if __name__ == "__main__":
    main()
