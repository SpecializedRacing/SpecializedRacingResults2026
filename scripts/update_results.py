#!/usr/bin/env python3
"""
Phase 2 automated results collector for the Specialized Racing Dashboard.

Design goals:
- Keep roster.json as the athlete/source-of-truth layer.
- Preserve existing data/results.json records.
- Pull public profile/result pages where available.
- Add only high-confidence records to data/results.json.
- Put uncertain matches into data/discovered_results_review.json for review.

This is intentionally conservative. It is better to miss a result than to publish a false record.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ROSTER_FILE = DATA / "roster.json"
RESULTS_FILE = DATA / "results.json"
SOURCES_FILE = DATA / "sources.json"
REVIEW_FILE = DATA / "discovered_results_review.json"

MAX_POSITION = 30
REQUEST_TIMEOUT = 25
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.6"))
MAX_ATHLETES_PER_RUN = int(os.getenv("MAX_ATHLETES_PER_RUN", "0"))  # 0 means full roster
SINCE_YEAR = os.getenv("RESULT_YEAR", "2026")

HEADERS = {
    "User-Agent": "SpecializedRacingDashboard/2.0 (+https://specializedracing.github.io/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DATE_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})\b")
INT_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$", re.I)


def norm(value: str) -> str:
    value = value or ""
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def slug(value: str) -> str:
    return norm(value).replace(" ", "-")


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def result_key(r: dict) -> str:
    return "|".join([
        str(r.get("date", "")),
        norm(r.get("race", "")),
        norm(r.get("athlete", "")),
        str(r.get("pos", "")),
    ])


def parse_pos(value: str) -> Optional[int]:
    value = str(value or "").strip()
    m = INT_RE.match(value)
    if not m:
        return None
    p = int(m.group(1))
    return p if 1 <= p <= MAX_POSITION else None


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    # keep value if it includes the target year; dashboard can still display it
    return value if SINCE_YEAR in value else ""


def fetch(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None
    finally:
        if REQUEST_PAUSE_SECONDS:
            time.sleep(REQUEST_PAUSE_SECONDS)


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def athlete_names(a: dict) -> List[str]:
    return [x for x in [a.get("rider", "")] + (a.get("aliases") or []) if x]


def build_profile_urls(a: dict) -> List[Tuple[str, str]]:
    urls: List[Tuple[str, str]] = []
    profile = a.get("profileUrl") or ""
    if profile:
        urls.append((profile, "profileUrl"))

    rider = a.get("rider", "")
    disc = (a.get("primaryDiscipline") or "").lower()
    rider_slug = slug(rider)

    # Road fallback: try a likely PCS rider profile URL when no PCS URL exists in roster.
    if rider_slug and disc == "road":
        urls.append((f"https://www.procyclingstats.com/rider/{rider_slug}", "pcs_slug_guess"))

    # MTB/DH fallback: MTBData rider slugs often use normalized name slugs.
    if rider_slug and disc in {"mtb", "dh"}:
        urls.append((f"https://mtbdata.com/riders/{rider_slug}", "mtbdata_slug_guess"))

    # De-dupe preserving order
    seen = set()
    out = []
    for url, source in urls:
        if url not in seen:
            seen.add(url)
            out.append((url, source))
    return out


def parse_table_rows(html: str, athlete: dict, url: str, url_source: str) -> Tuple[List[dict], List[dict]]:
    soup = BeautifulSoup(html, "lxml")
    high_confidence: List[dict] = []
    review: List[dict] = []
    names = [norm(n) for n in athlete_names(athlete)]
    rider_display = athlete.get("rider", "")

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        joined = " | ".join(cells)
        joined_norm = norm(joined)

        # Profile pages often omit the athlete name in result rows; source URL gives identity.
        contains_name = any(n and n in joined_norm for n in names)

        positions = [(i, parse_pos(c)) for i, c in enumerate(cells)]
        positions = [(i, p) for i, p in positions if p is not None]
        if not positions:
            continue
        pos_i, pos = positions[0]

        date = ""
        for c in cells:
            m = DATE_RE.search(c)
            if m:
                date = normalize_date(m.group(1))
                break
        if date and SINCE_YEAR not in date:
            continue

        # Pick the most likely race field: non-date, non-position, longer text.
        candidates = []
        for i, c in enumerate(cells):
            if i == pos_i:
                continue
            if DATE_RE.search(c):
                continue
            if parse_pos(c) is not None:
                continue
            if len(c) >= 4:
                candidates.append(c)
        race = max(candidates, key=len) if candidates else ""

        record = {
            "date": date,
            "race": race,
            "athlete": rider_display,
            "pos": pos,
            "src": url_source,
            "sourceUrl": url,
            "sourceId": url_source,
            "program": athlete.get("program", ""),
            "discipline": athlete.get("primaryDiscipline", ""),
            "team": athlete.get("team", ""),
        }

        if date and race and (contains_name or url_source in {"profileUrl", "pcs_slug_guess", "mtbdata_slug_guess"}):
            high_confidence.append(record)
        elif contains_name or race:
            record["rawRow"] = joined
            record["needsReview"] = True
            review.append(record)

    return high_confidence, review


def scan_text_for_result_lines(html: str, athlete: dict, url: str, source_id: str) -> Tuple[List[dict], List[dict]]:
    """Lightweight parser for source index/home pages.

    This looks for lines containing roster names/aliases plus a position number.
    It only promotes records with date + race + position. Others go to review.
    """
    text = page_text(html)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    names = [norm(n) for n in athlete_names(athlete)]
    high, review = [], []
    context_race = ""
    context_date = ""

    for line in lines:
        dm = DATE_RE.search(line)
        if dm:
            context_date = normalize_date(dm.group(1))
            # Often the date line also contains race name; keep it for context.
            context_race = re.sub(DATE_RE, "", line).strip(" -–|,") or context_race
        elif len(line) > 8 and not parse_pos(line):
            # keep possible race heading if not too generic
            if any(word in line.lower() for word in ["championship", "world cup", "tour", "classic", "series", "stage", "race", "results"]):
                context_race = line

        nline = norm(line)
        if not any(name and name in nline for name in names):
            continue
        tokens = line.replace("|", " ").split()
        pos = None
        for tok in tokens[:6]:
            pos = parse_pos(tok)
            if pos:
                break
        if not pos:
            m = re.search(r"\b([1-9]|[12][0-9]|30)(?:st|nd|rd|th)?\b", line, flags=re.I)
            if m:
                pos = int(m.group(1))
        if not pos:
            continue

        rec = {
            "date": context_date,
            "race": context_race,
            "athlete": athlete.get("rider", ""),
            "pos": pos,
            "src": source_id,
            "sourceUrl": url,
            "sourceId": source_id,
            "rawLine": line,
            "program": athlete.get("program", ""),
            "discipline": athlete.get("primaryDiscipline", ""),
            "team": athlete.get("team", ""),
        }
        if rec["date"] and rec["race"]:
            high.append(rec)
        else:
            rec["needsReview"] = True
            review.append(rec)
    return high, review


def collect_from_profile_pages(roster: List[dict]) -> Tuple[List[dict], List[dict]]:
    added, review = [], []
    athletes = roster[:MAX_ATHLETES_PER_RUN] if MAX_ATHLETES_PER_RUN else roster
    for a in athletes:
        for url, url_source in build_profile_urls(a):
            html = fetch(url)
            if not html:
                continue
            hi, rv = parse_table_rows(html, a, url, url_source)
            added.extend(hi)
            review.extend(rv[:5])  # keep review file manageable
    return added, review


def collect_from_source_indexes(roster: List[dict], sources_payload: dict) -> Tuple[List[dict], List[dict]]:
    added, review = [], []
    index_urls = []
    for s in sources_payload.get("sources", []):
        if not s.get("enabled", True):
            continue
        url = s.get("url")
        if url:
            index_urls.append((url, s.get("id", s.get("name", url))))
    for url, source_id in index_urls:
        html = fetch(url)
        if not html:
            continue
        for a in roster:
            hi, rv = scan_text_for_result_lines(html, a, url, source_id)
            added.extend(hi)
            review.extend(rv[:3])
    return added, review


def valid_record(r: dict) -> bool:
    try:
        pos = int(r.get("pos"))
    except Exception:
        return False
    return bool(r.get("date") and r.get("race") and r.get("athlete") and 1 <= pos <= MAX_POSITION)


def main() -> int:
    roster_payload = load_json(ROSTER_FILE, {"athletes": []})
    sources_payload = load_json(SOURCES_FILE, {"sources": []})
    results_payload = load_json(RESULTS_FILE, {"results": []})

    roster = roster_payload.get("athletes", [])
    existing = results_payload.get("results", [])
    existing_keys = {result_key(r) for r in existing}

    profile_added, profile_review = collect_from_profile_pages(roster)
    index_added, index_review = collect_from_source_indexes(roster, sources_payload)

    candidates = profile_added + index_added
    additions = []
    for r in candidates:
        if not valid_record(r):
            continue
        k = result_key(r)
        if k not in existing_keys:
            additions.append(r)
            existing_keys.add(k)

    combined = existing + additions
    combined.sort(key=lambda r: (str(r.get("date", "")), str(r.get("race", "")), str(r.get("athlete", ""))))

    output = {
        "lastUpdated": datetime.now(timezone.utc).date().isoformat(),
        "generatedBy": "scripts/update_results.py",
        "resultCount": len(combined),
        "addedThisRun": len(additions),
        "results": combined,
        "schema": {
            "date": "YYYY-MM-DD",
            "race": "string",
            "athlete": "name matching roster rider or alias",
            "pos": "number 1-30",
            "src": "source label",
            "sourceUrl": "optional evidence URL",
            "sourceId": "optional source registry id"
        }
    }
    save_json(RESULTS_FILE, output)

    review_payload = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Potential matches that were not confident enough to publish automatically.",
        "candidateCount": len(profile_review) + len(index_review),
        "candidates": profile_review + index_review,
    }
    save_json(REVIEW_FILE, review_payload)

    print(f"Roster athletes scanned: {len(roster[:MAX_ATHLETES_PER_RUN] if MAX_ATHLETES_PER_RUN else roster)}")
    print(f"Existing results: {len(existing)}")
    print(f"High-confidence candidates: {len(candidates)}")
    print(f"Added this run: {len(additions)}")
    print(f"Review candidates: {review_payload['candidateCount']}")
    print(f"Total results: {len(combined)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
