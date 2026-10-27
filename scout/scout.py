"""Grok scout, two modes. Runs in GitHub Actions; needs XAI_API_KEY.

detect:   cheap scan. open X search every run; handle lanes once a day (morning run).
          adds prices + DefiLlama movers. writes briefs/YYYY-MM-DD-HHMM.md
research: deep dive on the stories the drafter picked (STORIES env, JSON list).
          one targeted X search per story + DefiLlama protocol data if a slug is given.
          writes research/YYYY-MM-DD-HHMM.md
"""

import csv
import datetime as dt
import json
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
RESEARCH = ROOT / "research"
COSTS = BRIEFS / "costs.csv"
MYT = ZoneInfo("Asia/Kuala_Lumpur")
XAI_URL = "https://api.x.ai/v1/responses"
TICKS_PER_USD = 10_000_000_000

ITEM_FORMAT = """### <short title>
- importance: <high / medium / low>
- what happened: <facts, numbers, names; no speculation beyond the posts>
- why it matters: <who it affects, where the opportunity or risk is>
- signal: <how much discussion, who is talking about it>
- key post: <url> | @<author> (~<followers> followers) | posted <YYYY-MM-DD HH:MM> UTC | <likes>/<reposts>/<replies>
- other posts: <urls>"""

DETECT_PROMPT = """you are a research scout for a crypto writer. search X for posts since {since} (UTC).

lane: {name}
focus: {focus}

always report the top {n} developments you found, most important first, even if modest. the writer decides what's worth covering; your job is to surface, not filter. a single post with a real take, data point, announcement, or warning counts. only include things posted after {since}.

credibility: prefer official project accounts, known researchers, and posts with real engagement. ignore low-follower accounts with little engagement, memecoin shilling, giveaways, and engagement farming.

be concise. use exactly this format per item:

{fmt}

reply "nothing found" only if the search returned zero relevant posts in the window."""

REPLY_TARGETS_PROMPT = """find X posts published after {since} (UTC) that would be good to reply to for visibility.

topics: {focus}

a good target: posted after {since}, from an account with a large following (roughly 20k+ followers) or an official
project account, with replies and likes already building, where a knowledgeable person could add a sharp take.
skip scams, giveaways, memecoin shills, ragebait and engagement-farming accounts.

return up to 10, best first, exactly in this format:
### <topic>
- post: <url>
- author: @<handle> (~<followers> followers)
- posted: <YYYY-MM-DD HH:MM> UTC
- engagement: <likes>/<reposts>/<replies>
- gist: <one line on what the post says>
- why: <one line on why it's a good reply target>

reply "nothing found" only if there are zero suitable posts."""

ENGAGEMENT_PROMPT = """look up these X posts and report their current public metrics. also report the current follower count of @{handle}.

posts:
{urls}

format exactly:
followers: <number>
---
url: <url>
likes: <n>
reposts: <n>
replies: <n>
quotes: <n>
views: <n or unknown>
(one block per post. use "unknown" when a number isn't visible. do not estimate.)"""


RESEARCH_PROMPT = """you are a research assistant for a crypto writer who is about to post about this story.

story: {title}
what we know: {summary}
original post: {url}

search X (use at most 3 searches) and report, with links for every claim:
1. the original claim or announcement in full detail: numbers, dates, mechanism
2. primary sources: official accounts, docs, blogs, post-mortems
3. reactions from credible accounts: support and pushback
4. the counter-narrative or overlooked angle: what most people are missing or getting wrong
5. open questions and anything unverified

be concise. bullets only. no speculation beyond the sources."""


def log(msg):
    print(msg, flush=True)


def stamp(now_utc):
    return f"{now_utc.astimezone(MYT):%Y-%m-%d-%H%M}"


def log_cost(now_utc, kind, cost, calls, ok):
    new = not COSTS.exists()
    COSTS.parent.mkdir(exist_ok=True)
    with COSTS.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["generated_at_utc", "kind", "cost_usd", "x_search_calls", "ok"])
        w.writerow([now_utc.isoformat(), kind, f"{cost:.4f}", calls, ok])


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


def xai(model, prompt, tool, api_key, label):
    body = {"model": model, "input": [{"role": "user", "content": prompt}], "tools": [tool]}
    err = None
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
            log(f"[ok] {label}: ${cost:.4f}, {calls} tool calls")
            return {"text": text, "links": links, "cost": cost, "calls": calls, "error": None}
        except Exception as e:  # noqa: BLE001
            err = str(e)
            log(f"[retry {attempt}] {label}: {err}")
    return {"text": "", "links": [], "cost": 0.0, "calls": 0, "error": err}


def x_tool(since, now_utc, handles=None):
    # to_date behaves as exclusive (a same-day to_date drops today's posts), so use tomorrow
    tool = {"type": "x_search", "from_date": since.strftime("%Y-%m-%d"),
            "to_date": (now_utc + dt.timedelta(days=1)).strftime("%Y-%m-%d")}
    if handles:
        tool["allowed_x_handles"] = handles[:10]
    return tool


# ---------- detect ----------

def last_brief_time():
    for p in sorted(BRIEFS.glob("20*.md"), reverse=True):
        m = re.search(r"^generated_at: (\S+)$", p.read_text(), re.M)
        if m:
            return dt.datetime.fromisoformat(m.group(1))
    return None


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
            return (f"- {p['name']} [slug: {p.get('slug')}] ({p.get('category', '?')}): "
                    f"${p['tvl'] / 1e6:,.0f}M TVL, {p['change_1d']:+.1f}% 1d{c7s}")

        up = sorted(protos, key=lambda p: p["change_1d"], reverse=True)[:8]
        down = sorted(protos, key=lambda p: p["change_1d"])[:8]
        opt = sorted([p for p in protos if (p.get("category") or "").lower() in
                      ("options", "options vault", "derivatives")], key=lambda p: p["tvl"], reverse=True)[:10]
        lines += ["", "**biggest TVL gainers (1d, TVL >= $20M)**"] + [row(p) for p in up]
        lines += ["", "**biggest TVL drops (1d, TVL >= $20M)**"] + [row(p) for p in down]
        lines += ["", "**options / derivatives protocols by TVL**"] + [row(p) for p in opt]
    except Exception as e:  # noqa: BLE001
        lines.append(f"- DefiLlama unavailable: {e}")
    return lines


def detect(cfg, api_key, now_utc):
    lanes_mode = (os.environ.get("LANES") or "").strip()
    if lanes_mode not in ("all", "open"):
        lanes_mode = "all" if now_utc.astimezone(MYT).hour < cfg.get("handle_lanes_before_hour_myt", 10) else "open"
    lanes = [l for l in cfg["lanes"] if lanes_mode == "all" or not l.get("handles") or l.get("every_run")]

    prev = last_brief_time()
    open_since = max(prev or now_utc - dt.timedelta(hours=12), now_utc - dt.timedelta(hours=cfg["max_window_hours"]))
    handle_since = now_utc - dt.timedelta(hours=24)

    def run(lane):
        if lane.get("kind") == "reply_targets":
            since = now_utc - dt.timedelta(hours=lane.get("window_hours", 3))
            prompt = REPLY_TARGETS_PROMPT.format(since=since.strftime("%Y-%m-%d %H:%M"), focus=lane["focus"])
        else:
            since = open_since if (not lane.get("handles") or lane.get("every_run")) else handle_since
            prompt = DETECT_PROMPT.format(since=since.strftime("%Y-%m-%d %H:%M"), name=lane["name"],
                                          focus=lane["focus"], n=lane.get("items", 5), fmt=ITEM_FORMAT)
        res = xai(cfg["model"], prompt, x_tool(since, now_utc, lane.get("handles")), api_key, lane["name"])
        return lane, since, res

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, lanes))

    ok = [r for r in results if not r[2]["error"]]
    cost = sum(r[2]["cost"] for r in results)
    calls = sum(r[2]["calls"] for r in results)
    log_cost(now_utc, f"detect-{lanes_mode}", cost, calls, f"{len(ok)}/{len(results)}")
    if not ok:
        print(f"::error::all lanes failed: {results[0][2]['error']}", flush=True)
        sys.exit(1)

    now_myt = now_utc.astimezone(MYT)
    out = ["---", f"generated_at: {now_utc.isoformat()}", f"lanes: {lanes_mode}",
           f"model: {cfg['model']}", f"cost_usd: {cost:.4f}", f"x_search_calls: {calls}",
           f"lanes_ok: {len(ok)}/{len(results)}", "---", "",
           f"# brief {now_myt:%Y-%m-%d %H:%M} MYT ({lanes_mode} lanes)", "",
           "## market data", *market_data(), ""]
    for lane, since, res in results:
        out.append(f"## lane: {lane['name']} (since {since.astimezone(MYT):%m-%d %H:%M} MYT)")
        out.append(f"_lane failed: {res['error']}_" if res["error"] else (res["text"] or "nothing found"))
        out.append("")
    path = BRIEFS / f"{stamp(now_utc)}.md"
    path.write_text("\n".join(out))
    log(f"wrote {path.relative_to(ROOT)}; ${cost:.4f}")


# ---------- research ----------

def defillama_protocol(slug):
    try:
        r = requests.get(f"https://api.llama.fi/protocol/{slug}", timeout=60)
        r.raise_for_status()
        d = r.json()
        tvl = d.get("tvl") or []
        pts = {dt.datetime.fromtimestamp(p["date"], dt.timezone.utc).date(): p["totalLiquidityUSD"] for p in tvl[-31:]}
        days = sorted(pts)
        lines = [f"- DefiLlama {d.get('name')} ({d.get('category')}), chains: {', '.join(d.get('chains', [])[:6])}"]
        if days:
            last = pts[days[-1]]
            for back in (1, 7, 30):
                if len(days) > back:
                    prev = pts[days[-1 - back]]
                    if prev:
                        lines.append(f"  - TVL ${last / 1e6:,.1f}M, {(last / prev - 1) * 100:+.1f}% vs {back}d ago")
        return lines
    except Exception as e:  # noqa: BLE001
        return [f"- DefiLlama {slug}: unavailable ({e})"]


def research(cfg, api_key, now_utc):
    stories = json.loads(os.environ.get("STORIES") or "[]")
    if not stories:
        sys.exit("STORIES is empty")
    since = now_utc - dt.timedelta(days=7)

    def run(s):
        res = {"text": "", "links": [], "cost": 0.0, "calls": 0, "error": None}
        if cfg.get("research_x", True):
            prompt = RESEARCH_PROMPT.format(title=s.get("title", ""), summary=s.get("summary", ""),
                                            url=s.get("url", "n/a"))
            res = xai(cfg["model"], prompt, x_tool(since, now_utc), api_key, f"research: {s.get('title')}")
        llama = defillama_protocol(s["defillama_slug"]) if s.get("defillama_slug") else []
        return s, res, llama

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, stories[:5]))

    cost = sum(r[1]["cost"] for r in results)
    calls = sum(r[1]["calls"] for r in results)
    ok = sum(1 for r in results if not r[1]["error"])
    log_cost(now_utc, "research", cost, calls, f"{ok}/{len(results)}")

    out = ["---", f"generated_at: {now_utc.isoformat()}", f"cost_usd: {cost:.4f}", "---", ""]
    for s, res, llama in results:
        out.append(f"## {s.get('id', '')} {s.get('title', '')}".strip())
        out += llama
        out.append(f"_x research failed: {res['error']}_" if res["error"] else res["text"])
        out.append("")
    RESEARCH.mkdir(exist_ok=True)
    path = RESEARCH / f"{stamp(now_utc)}.md"
    path.write_text("\n".join(out))
    log(f"wrote {path.relative_to(ROOT)}; ${cost:.4f}")


# ---------- engagement ----------

def engagement(cfg, api_key, now_utc):
    urls = json.loads(os.environ.get("STORIES") or "[]")
    handle = (os.environ.get("HANDLE") or "insomniac988").lstrip("@")
    if not urls:
        sys.exit("STORIES (list of post urls) is empty")
    since = now_utc - dt.timedelta(days=30)
    res = xai(cfg["model"], ENGAGEMENT_PROMPT.format(handle=handle, urls="\n".join(urls[:30])),
              x_tool(since, now_utc), api_key, "engagement")
    log_cost(now_utc, "engagement", res["cost"], res["calls"], "ok" if not res["error"] else "fail")
    RESEARCH.mkdir(exist_ok=True)
    path = RESEARCH / f"engagement-{stamp(now_utc)}.md"
    path.write_text(f"---\ngenerated_at: {now_utc.isoformat()}\n---\n\n" + (res["text"] or f"_failed: {res['error']}_"))
    log(f"wrote {path.relative_to(ROOT)}")


# ---------- voice ----------

VOICE_PROMPT = """search X for {kind} by @{handle} and return them VERBATIM. copy the exact text character for character:
same casing, spelling, slang, abbreviations, punctuation, emojis and line breaks. do not paraphrase, summarise, fix typos or translate.
return up to {n} of the most recent ones, newest first. use at most 5 searches.

format for each:
---
date: <YYYY-MM-DD>
url: <post url>
{extra}text:
<exact text>"""


def voice(cfg, api_key, now_utc):
    handle = (os.environ.get("HANDLE") or "insomniac988").lstrip("@")
    since = now_utc - dt.timedelta(days=120)
    jobs = [("original posts (not replies, not reposts)", "", "posts"),
            ("replies to other accounts", "replying to: <@handle and a one-line gist of the post being replied to>\n", "replies")]
    out = ["---", f"generated_at: {now_utc.isoformat()}", f"handle: {handle}", "---", ""]
    total = 0.0
    for kind, extra, label in jobs:
        res = xai(cfg["model"], VOICE_PROMPT.format(kind=kind, handle=handle, n=40, extra=extra),
                  x_tool(since, now_utc, [handle]), api_key, f"voice {label}")
        total += res["cost"]
        out += [f"## {label}", res["text"] or f"_failed: {res['error']}_", ""]
    log_cost(now_utc, "voice", total, 0, "-")
    d = ROOT / "voice"
    d.mkdir(exist_ok=True)
    (d / f"{handle}-corpus.md").write_text("\n".join(out))
    log(f"wrote voice/{handle}-corpus.md; ${total:.4f}")


HISTORY_PROMPT = """search X for posts by @{handle} published between {start} and {end}. include original posts,
quote posts, and replies longer than ~80 characters. skip one-liners, emoji-only replies and reposts.
return them VERBATIM: exact text character for character, same casing, slang, punctuation, emojis and line breaks.
do not paraphrase, summarise or fix anything. return up to 25, spread across the whole period. use at most 4 searches.

format for each:
---
date: <YYYY-MM-DD>
url: <post url>
type: <post | quote | reply>
text:
<exact text>"""


def history(cfg, api_key, now_utc):
    handle = (os.environ.get("HANDLE") or "insomniac988").lstrip("@")
    # optional params via the `stories` input: {"windows": [["2023-01-01","2023-12-31"]], "topic": "...", "name": "..."}
    params = json.loads(os.environ.get("STORIES") or "{}") if (os.environ.get("STORIES") or "").strip().startswith("{") else {}
    topic = params.get("topic", "")
    windows = []
    if params.get("windows"):
        for a, b in params["windows"]:
            windows.append((dt.datetime.fromisoformat(a).replace(tzinfo=dt.timezone.utc),
                            dt.datetime.fromisoformat(b).replace(tzinfo=dt.timezone.utc)))
    else:
        for i in range(int(os.environ.get("MONTHS") or 6)):
            end = now_utc - dt.timedelta(days=30 * i)
            windows.append((end - dt.timedelta(days=30), end))

    def run(w):
        start, end = w
        tool = {"type": "x_search", "allowed_x_handles": [handle],
                "from_date": start.strftime("%Y-%m-%d"), "to_date": (end + dt.timedelta(days=1)).strftime("%Y-%m-%d")}
        prompt = HISTORY_PROMPT.format(handle=handle, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if topic:
            prompt = prompt.replace("published between", f"about {topic}, published between")
        return w, xai(cfg["model"], prompt, tool, api_key, f"history {start:%Y-%m-%d}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, windows))
    total = sum(r[1]["cost"] for r in results)
    log_cost(now_utc, "history", total, sum(r[1]["calls"] for r in results), "-")
    out = ["---", f"generated_at: {now_utc.isoformat()}", f"handle: {handle}", f"topic: {topic or 'all'}", f"windows: {len(windows)}", "---", ""]
    for (start, end), res in results:
        out += [f"## {start:%Y-%m-%d} to {end:%Y-%m-%d}", res["text"] or f"_failed: {res['error']}_", ""]
    d = ROOT / "voice"
    d.mkdir(exist_ok=True)
    name = params.get("name") or "history"
    (d / f"{handle}-{name}.md").write_text("\n".join(out))
    log(f"wrote voice/{handle}-{name}.md; ${total:.4f}")


def main():
    api_key = (os.environ.get("XAI_API_KEY") or "").strip().strip("'\"").strip()
    if not api_key:
        sys.exit("XAI_API_KEY not set")
    cfg = yaml.safe_load((ROOT / "scout" / "lanes.yaml").read_text())
    if os.environ.get("XAI_MODEL"):
        cfg["model"] = os.environ["XAI_MODEL"].strip()
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    mode = (os.environ.get("MODE") or "detect").strip()
    if mode == "research":
        research(cfg, api_key, now_utc)
    elif mode == "voice":
        voice(cfg, api_key, now_utc)
    elif mode == "history":
        history(cfg, api_key, now_utc)
    elif mode == "engagement":
        engagement(cfg, api_key, now_utc)
    else:
        detect(cfg, api_key, now_utc)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # surface the real error as a run annotation
        import traceback
        tb = traceback.format_exc()
        print(tb, flush=True)
        print("::error::" + tb.strip().splitlines()[-1], flush=True)
        sys.exit(1)
