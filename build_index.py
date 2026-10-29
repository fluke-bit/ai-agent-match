#!/usr/bin/env python3
"""
Build a compact agents-index.json from public agent directories:
  - GrokBot HQ and Grokdex  -> "Grok Bots"
  - awesome-muse-skills     -> "Muse" (community skills; no install counts)
Run periodically (daily is fine). No API keys required.

Muse notes:
  - The Muse catalog has no popularity data, so Muse entries are NOT scored;
    they sort alphabetically after the scored Grok Bots in the index.
  - Each entry keeps its license. Entries with no license are skipped
    (set MUSE_REQUIRE_LICENSE = False to keep them as "Unspecified").
  - Muse has no one-click add, so each entry carries a setupNote and a
    "View skill" call-to-action that the page displays.

Fixes vs. the original:
  - null-safe text handling (no more None[:140] crashes)
  - tolerant of list-or-dict API responses
  - one failed source no longer kills the run
  - URL normalization before de-duplication
  - entries without a name or a valid https link are dropped
  - per-source percentile scoring so no source dominates the ranking
  - ease estimate uses thresholds (no banker's rounding) and a real routines flag
  - 'source' values match the PWA filter buttons ("Grok Bots", "Muse", "Other")
  - timezone-aware timestamps, UTF-8 output, atomic write, retries, User-Agent
"""

import json
import os
import re
import sys
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

HQ_URL = "https://grokbothq.xyz/api/v1/index.json"
GROKDEX_URL = "https://grokdex.net/api/bots"
MUSE_URL = "https://raw.githubusercontent.com/aicodedecode/awesome-muse-skills/main/skills.json"
OUTPUT = "agents-index.json"
MAX_AGENTS = 1000       # cap for Grok Bots (kept by usefulness score)
MAX_MUSE = 1500         # cap for Muse skills (kept alphabetically)
MUSE_REQUIRE_LICENSE = True  # skip Muse entries that declare no license

PLATFORM = "Grok Bots"  # must match data-platform on the PWA filter buttons
MUSE_PLATFORM = "Muse"  # must match data-platform on the PWA filter buttons
MUSE_NOTE = ("Not a one-click add: open the skill, copy its SKILL.md into a Muse chat, "
             "and ask Muse to use it.")
HEADERS = {"User-Agent": "AIAgentMatch-IndexBuilder/1.0 (+https://github.com/fluke-bit)"}
RETRIES = 3
TIMEOUT = 60

TAG_RE = re.compile(r"<[^>]*>")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
WS_RE = re.compile(r"\s+")


# ---------- helpers ----------

def clean_text(value, limit=None):
    """Return a safe, single-spaced string (never None)."""
    if value is None:
        return ""
    s = str(value)
    s = TAG_RE.sub("", s)
    s = CTRL_RE.sub("", s)
    s = WS_RE.sub(" ", s).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "\u2026"
    return s


def to_int(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def normalize_url(url):
    """Canonical https URL (no query, fragment, or trailing slash), or None."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def clean_list(value, max_items=20):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or item.get("slug")
        t = clean_text(item, 40)
        if t and t not in out:
            out.append(t)
        if len(out) >= max_items:
            break
    return out


def fetch(url):
    """GET JSON with retries. Returns parsed JSON or None on failure."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  attempt {attempt}/{RETRIES} failed for {url}: {e}", file=sys.stderr)
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    return None


def extract_bots(payload):
    """Accept either a bare list or a dict wrapping the list."""
    if isinstance(payload, list):
        return [b for b in payload if isinstance(b, dict)]
    if isinstance(payload, dict):
        for key in ("bots", "agents", "items", "data", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [b for b in val if isinstance(b, dict)]
    return []


# ---------- ease estimate ----------

def estimate_ease(agent):
    """1 = Easy, 2 = Medium, 3 = Advanced. Threshold-based, no rounding."""
    points = 0.0
    if len(agent.get("integrations") or []) >= 3:
        points += 1
    text = f"{agent.get('description', '')} {agent.get('tagline', '')}".lower()
    if any(k in text for k in ("api key", "token", "custom", "advanced")):
        points += 1
    if agent.get("hasRoutines"):
        points += 0.5
    if points < 1:
        return 1
    if points < 2:
        return 2
    return 3


# ---------- normalizers ----------

def normalize_hq(bot):
    url = normalize_url(bot.get("open"))
    name = clean_text(bot.get("name"), 80)
    if not url or not name:
        return None
    desc = clean_text(bot.get("description"), 600)
    return {
        "id": clean_text(bot.get("slug") or url, 200),
        "name": name,
        "tagline": clean_text(bot.get("tagline"), 140) or clean_text(desc, 140),
        "description": desc,
        "category": clean_text(bot.get("category"), 40) or "General",
        "integrations": clean_list(bot.get("integrations") or bot.get("skills")),
        "hasRoutines": bool(bot.get("routines")),
        "installs": to_int(bot.get("installs")),
        "_popularity": to_int(bot.get("installs")),  # HQ only exposes installs
        "openUrl": url,
        "source": PLATFORM,
        "listedOn": "GrokBot HQ",
        "sourcePage": normalize_url(bot.get("page")),
        "builder": clean_text(bot.get("builder"), 60),
        "added": clean_text(bot.get("added"), 40),
    }


def normalize_dex(bot):
    url = normalize_url(bot.get("botUrl"))
    name = clean_text(bot.get("title"), 80)
    if not url or not name:
        return None
    desc = clean_text(bot.get("description"), 600)
    adds = to_int(bot.get("adds"))
    return {
        "id": clean_text(bot.get("slug") or url, 200),
        "name": name,
        "tagline": clean_text(bot.get("summary"), 140) or clean_text(desc, 140),
        "description": desc,
        "category": clean_text(bot.get("lane"), 40) or "General",
        "integrations": clean_list(bot.get("skills") or bot.get("tags")),
        "hasRoutines": bool(bot.get("routines")),
        "installs": adds,
        "_popularity": adds + to_int(bot.get("score")),
        "openUrl": url,
        "source": PLATFORM,
        "listedOn": "Grokdex",
        "sourcePage": normalize_url(bot.get("listingUrl")),
        "builder": clean_text(bot.get("authorName") or bot.get("xHandle"), 60),
        "added": clean_text(bot.get("createdAt"), 40),
    }


def humanize(slug):
    """'3d-web-experiences' -> '3D Web Experiences' (for display only)."""
    words = clean_text(slug, 80).replace("_", "-").split("-")
    return " ".join(w[:1].upper() + w[1:] for w in words if w)


def normalize_muse(bot):
    url = normalize_url(bot.get("github_url"))
    raw_name = clean_text(bot.get("name"), 80)
    if not url or not raw_name:
        return None
    lic = clean_text(bot.get("license"), 40)
    if not lic:
        if MUSE_REQUIRE_LICENSE:
            return None
        lic = "Unspecified"
    desc = clean_text(bot.get("description"), 600)
    return {
        "id": clean_text(bot.get("path") or url, 200),
        "name": humanize(raw_name),
        "tagline": clean_text(desc, 140),
        "description": desc,
        "category": clean_text(bot.get("category"), 40) or "General",
        "integrations": [],
        "hasRoutines": False,
        "installs": 0,        # the catalog has no popularity data
        "_popularity": 0,
        "_pct": 0.0,          # unscored: Muse is never percentile-ranked
        "_ease": 2,           # manual copy-and-paste setup = Medium
        "openUrl": url,
        "source": MUSE_PLATFORM,
        "listedOn": "awesome-muse-skills",
        "sourcePage": url,
        "builder": clean_text(bot.get("author"), 60),
        "added": "",
        "license": lic,
        "kind": "skill",
        "cta": "View skill",
        "setupNote": MUSE_NOTE,
    }


# ---------- scoring ----------

def add_percentiles(agents):
    """Score 0-100 = percentile of popularity within the agent's own source."""
    values = sorted(a["_popularity"] for a in agents)
    n = len(values)
    for a in agents:
        if n <= 1:
            a["_pct"] = 50.0
            continue
        lo = bisect_left(values, a["_popularity"])
        hi = bisect_right(values, a["_popularity"])
        a["_pct"] = round(100.0 * (lo + 0.5 * (hi - lo)) / n, 2)


def merge(primary, secondary):
    """Fill blanks in primary from secondary; keep best popularity signals."""
    for key in ("tagline", "description", "builder", "added", "sourcePage"):
        if not primary.get(key) and secondary.get(key):
            primary[key] = secondary[key]
    if primary.get("category") == "General" and secondary.get("category") != "General":
        primary["category"] = secondary["category"]
    for item in secondary.get("integrations", []):
        if item not in primary["integrations"] and len(primary["integrations"]) < 20:
            primary["integrations"].append(item)
    primary["hasRoutines"] = primary["hasRoutines"] or secondary["hasRoutines"]
    primary["installs"] = max(primary["installs"], secondary["installs"])
    primary["_pct"] = max(primary["_pct"], secondary["_pct"])
    primary["listedOn"] = f"{primary['listedOn']}, {secondary['listedOn']}"


# ---------- main ----------

def load_source(label, url, normalizer, scored=True):
    print(f"Fetching {label}...")
    payload = fetch(url)
    if payload is None:
        print(f"  {label}: FAILED, continuing without it", file=sys.stderr)
        return None
    raw = extract_bots(payload)
    items = [n for n in (normalizer(b) for b in raw) if n]
    print(f"  {label}: {len(raw)} raw, {len(items)} valid")
    if scored:
        add_percentiles(items)
    return items


def main():
    hq = load_source("GrokBot HQ", HQ_URL, normalize_hq)
    dex = load_source("Grokdex", GROKDEX_URL, normalize_dex)
    muse = load_source("Muse skills", MUSE_URL, normalize_muse, scored=False)

    sources_used = [
        name
        for name, data in (("GrokBot HQ", hq), ("Grokdex", dex), ("awesome-muse-skills", muse))
        if data
    ]
    if not sources_used:
        print("No sources succeeded; leaving existing index untouched.", file=sys.stderr)
        sys.exit(1)

    agents = {}
    # HQ first (more reviewed); Grokdex fills gaps and merges on duplicates
    for a in hq or []:
        agents[a["openUrl"]] = a
    for a in dex or []:
        existing = agents.get(a["openUrl"])
        if existing:
            merge(existing, a)
        else:
            agents[a["openUrl"]] = a

    grok = list(agents.values())
    muse_items = {}
    for a in muse or []:
        muse_items.setdefault(a["openUrl"], a)  # first one wins on duplicate URLs

    for a in grok + list(muse_items.values()):
        a["ease"] = a.pop("_ease", None) or estimate_ease(a)
        a["usefulness"] = a["_pct"]
        del a["_pct"], a["_popularity"], a["hasRoutines"]

    # Grok Bots: best score first. Muse: alphabetical (no popularity data).
    # Caps apply per platform so one source can't crowd out the other.
    grok_ranked = sorted(grok, key=lambda x: (x["usefulness"], x["installs"]), reverse=True)[:MAX_AGENTS]
    muse_ranked = sorted(muse_items.values(), key=lambda x: x["name"].lower())[:MAX_MUSE]
    ranked = grok_ranked + muse_ranked

    out = {
        "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(ranked),
        "sources": sources_used,
        "agents": ranked,
    }

    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUTPUT)  # atomic: never leaves a half-written file
    print(f"Wrote {len(ranked)} agents to {OUTPUT} (sources: {', '.join(sources_used)})")


if __name__ == "__main__":
    main()
