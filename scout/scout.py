"""Grok scout: searches X per lane via xAI x_search, adds DefiLlama and price data,
writes a narrative brief to briefs/. Runs in GitHub Actions; needs XAI_API_KEY."""

import csv
import datetime as dt
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
BRIEFS = ROOT / "briefs"
MYT = ZoneInfo("Asia/Kuala_Lumpur")
XAI_URL = "https://api.x.ai/v1/responses"
TICKS_PER_USD = 10_000_000_000

LANE_PROMPT = """you are a research scout for a crypto writer. search X for what the accounts in scope posted since {since} (UTC).

lane: {name}
focus: {focus}

always report the top 5 developments you found, most important first, even if they seem modest. a writer will decide what's worth covering, so your job is to surface, not to filter. a single post with a real take, data point, announcement, or warning counts. only include things posted after {since}. use exactly this format:

### <short title>
- importance: <high / medium / low>
- what happened: <facts, numbers, names; no speculation beyond the posts>
- why it matters: <who it affects, where the opportunity or risk is>
- signal: <how much discussion / engagement, who is talking about it>
- posts: <links to the key X posts>

reply "nothing found" only if the search returned zero relevant posts in the window.
skip memecoin shilling, giveaways, and engagement farming."""


def log(msg):
    print(msg, flush=True)


def window_start(now_utc, max_hours):
    """Start from the previous brief's timestamp so delayed runs don't leave gaps."""
    floor = now_utc - dt.timedelta(hours=max_hours)
    prev = sorted(BRIEFS.glob("20*.md"))
    if prev:
        m = re.search(r"^generated_at: (\S+)$", prev[-1].read_text(), re.M)
        if m:
            return max(dt.datetime.fromisoformat(m.group(1)), floor)
    return max(now_utc - dt.timedelta(hours=12), floor)


def extract_text_and_links(data):
    text = data.get("output_text") or ""
    links = set(data.get("citations") or [])
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if part.get("type") == "output_text":
                if not data.get("output_text"):
                    text += part.get("text", "")
                for ann in part.get("annotations", []) or []:
                    if ann.get("url"):
                        links.add(ann["url"])
    return text.strip(), sorted(l for l in links if isinstance(l, str))


def run_lane(lane, model, since, now_utc, api_key):
    tool = {
        "type": "x_search",
        "from_date": since.strftime("%Y-%m-%d"),
        # to_date is exclusive in practice (a same-day to_date cut off today's posts), so use tomorrow
        "to_date": (now_utc + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    if lane.get("handles"):
        tool["allowed_x_handles"] = lane["handles"][:10]
    body = {
        "model": model,
        "input": [{"role": "user", "content": LANE_PROMPT.format(
            since=since.strftime("%Y-%m-%d %H:%M"), name=lane["name"], focus=lane["focus"])}],
        "tools": [tool],
    }
    last_err = None
    for attempt in (1, 2):
        try:
            r = requests.post(XAI_URL, json=body, timeout=240,
                              headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            text, links = extract_text_and_links(data)
            usage = data.get("usage") or {}
            cost = (usage.get("cost_in_usd_ticks") or 0) / TICKS_PER_USD
            calls = usage.get("num_server_side_tools_used") or 0
            log(f"[ok] {lane['name']}: ${cost:.4f}, {calls} tool calls")
            return {"lane": lane, "text": text, "links": links, "cost": cost, "calls": calls, "error": None}
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            log(f"[retry {attempt}] {lane['name']}: {last_err}")
    return {"lane": lane, "text": "", "links": [], "cost": 0.0, "calls": 0, "error": last_err}


def market_data():
    lines = []
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", timeout=30, params={
            "ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"})
        r.raise_for_status()
        p = r.json()
        for cg_id, sym in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")):
            d = p.get(cg_id, {})
            lines.append(f"- {sym}: ${d.get('usd', 0):,.2f} ({d.get('usd_24h_change', 0):+.2f}% 24h)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- prices unavailable: {e}")

    try:
        r = requests.get("https://api.llama.fi/protocols", timeout=60)
        r.raise_for_status()
        protos = [p for p in r.json() if (p.get("tvl") or 0) >= 20_000_000 and p.get("change_1d") is not None]

        def row(p):
            c7 = p.get("change_7d")
            c7s = f", {c7:+.1f}% 7d" if c7 is not None else ""
            return (f"- {p['name']} ({p.get('category', '?')}): ${p['tvl'] / 1e6:,.0f}M TVL, "
                    f"{p['change_1d']:+.1f}% 1d{c7s}")

        up = sorted(protos, key=lambda p: p["change_1d"], reverse=True)[:8]
        down = sorted(protos, key=lambda p: p["change_1d"])[:8]
        opt = sorted([p for p in protos if (p.get("category") or "").lower() in
                      ("options", "options vault", "derivatives")],
                     key=lambda p: p["tvl"], reverse=True)[:10]
        lines += ["", "**biggest TVL gainers (1d, TVL >= $20M)**"] + [row(p) for p in up]
        lines += ["", "**biggest TVL drops (1d, TVL >= $20M)**"] + [row(p) for p in down]
        lines += ["", "**options / derivatives protocols by TVL**"] + [row(p) for p in opt]
    except Exception as e:  # noqa: BLE001
        lines.append(f"- DefiLlama unavailable: {e}")
    return lines


def main():
    api_key = (os.environ.get("XAI_API_KEY") or "").strip().strip("'\"").strip()
    if not api_key:
        sys.exit("XAI_API_KEY not set")
    cfg = yaml.safe_load((ROOT / "scout" / "lanes.yaml").read_text())
    if os.environ.get("XAI_MODEL"):
        cfg["model"] = os.environ["XAI_MODEL"].strip()
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    since = window_start(now_utc, cfg.get("max_window_hours", 24))
    log(f"window: {since.isoformat()} -> {now_utc.isoformat()}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda lane: run_lane(lane, cfg["model"], since, now_utc, api_key), cfg["lanes"]))

    ok = [r for r in results if not r["error"]]
    if not ok:
        # no brief written, so the next run's window still starts at the last good brief
        print(f"::error::all lanes failed: {results[0]['error']}", flush=True)
        sys.exit(1)
    total_cost = sum(r["cost"] for r in results)
    total_calls = sum(r["calls"] for r in results)
    now_myt = now_utc.astimezone(MYT)

    out = [
        "---",
        f"generated_at: {now_utc.isoformat()}",
        f"window_start: {since.isoformat()}",
        f"model: {cfg['model']}",
        f"cost_usd: {total_cost:.4f}",
        f"x_search_calls: {total_calls}",
        f"lanes_ok: {len(ok)}/{len(results)}",
        "---",
        "",
        f"# brief {now_myt:%Y-%m-%d %H:%M} MYT",
        f"covers {since.astimezone(MYT):%Y-%m-%d %H:%M} to {now_myt:%H:%M} MYT",
        "",
        "## market data",
        *market_data(),
        "",
    ]
    for r in results:
        out.append(f"## lane: {r['lane']['name']}")
        if r["error"]:
            out.append(f"_lane failed: {r['error']}_")
        else:
            out.append(r["text"] or "nothing notable")
            if r["links"]:
                out += ["", "sources:"] + [f"- {l}" for l in r["links"]]
        out.append("")

    BRIEFS.mkdir(exist_ok=True)
    path = BRIEFS / f"{now_myt:%Y-%m-%d-%H%M}.md"
    path.write_text("\n".join(out))
    log(f"wrote {path.relative_to(ROOT)}; total ${total_cost:.4f}, {total_calls} tool calls")

    costs = BRIEFS / "costs.csv"
    new = not costs.exists()
    with costs.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["generated_at_utc", "cost_usd", "x_search_calls", "lanes_ok"])
        w.writerow([now_utc.isoformat(), f"{total_cost:.4f}", total_calls, f"{len(ok)}/{len(results)}"])


if __name__ == "__main__":
    main()
