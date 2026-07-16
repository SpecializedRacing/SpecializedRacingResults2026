#!/usr/bin/env python3
"""
Phase 3 results updater for Specialized Racing Dashboard.

Purpose:
- Preserve existing data/results.json.
- Scan roster profile URLs and source index pages.
- Publish records when athlete + race + position are found, even if no race date is parsed.
- Mark records with missing dates using dateStatus instead of inventing dates.
"""
from __future__ import annotations

import json, os, re, time, unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple
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
RESULT_YEAR = os.getenv("RESULT_YEAR", "2026")
REQUEST_TIMEOUT = 25
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.5"))
MAX_ATHLETES_PER_RUN = int(os.getenv("MAX_ATHLETES_PER_RUN", "0"))

HEADERS = {
    "User-Agent": "SpecializedRacingDashboard/3.0 (+GitHub Actions)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DATE_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|\d{1,2}\.\d{2})\b")
INT_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$", re.I)


def norm(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def slug(value: str) -> str:
    return norm(value).replace(" ", "-")


def load_json(path: Path, default):
    if not path.exists(): return default
    with path.open("r", encoding="utf-8") as f: return json.load(f)


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def result_key(r: dict) -> str:
    return "|".join([str(r.get("date", "")), norm(r.get("race", "")), norm(r.get("athlete", "")), str(r.get("pos", ""))])


def parse_pos(value: str) -> Optional[int]:
    m = INT_RE.match(str(value or "").strip())
    if not m: return None
    p = int(m.group(1))
    return p if 1 <= p <= MAX_POSITION else None


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    if not value: return ""
    # PCS profile rows sometimes give DD.MM without a year. Assume target year but mark downstream if needed.
    m = re.match(r"^(\d{1,2})\.(\d{2})$", value)
    if m:
        return f"{RESULT_YEAR}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try: return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except Exception: pass
    return value if RESULT_YEAR in value else ""


def fetch(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200: return None
        return r.text
    except Exception:
        return None
    finally:
        if REQUEST_PAUSE_SECONDS: time.sleep(REQUEST_PAUSE_SECONDS)


def athlete_names(a: dict) -> List[str]:
    return [x for x in [a.get("rider", "")] + (a.get("aliases") or []) if x]


def build_profile_urls(a: dict) -> List[Tuple[str, str]]:
    urls=[]
    profile=a.get("profileUrl") or ""
    if profile: urls.append((profile,"profileUrl"))
    rider_slug=slug(a.get("rider", ""))
    disc=(a.get("primaryDiscipline") or "").lower()
    if rider_slug and disc == "road": urls.append((f"https://www.procyclingstats.com/rider/{rider_slug}","pcs_slug_guess"))
    if rider_slug and disc in {"mtb","dh"}: urls.append((f"https://mtbdata.com/riders/{rider_slug}","mtbdata_slug_guess"))
    seen=set(); out=[]
    for u,s in urls:
        if u not in seen:
            seen.add(u); out.append((u,s))
    return out


def soup_text(html: str) -> str:
    soup=BeautifulSoup(html,"lxml")
    for tag in soup(["script","style","noscript"]): tag.decompose()
    return soup.get_text("\n", strip=True)


def parse_profile_tables(html: str, athlete: dict, url: str, source_id: str):
    soup=BeautifulSoup(html,"lxml")
    high=[]; review=[]
    names=[norm(n) for n in athlete_names(athlete)]
    for tr in soup.find_all("tr"):
        cells=[c.get_text(" ", strip=True) for c in tr.find_all(["td","th"])]
        if len(cells)<2: continue
        joined=" | ".join(cells)
        joined_norm=norm(joined)
        contains_name=any(n and n in joined_norm for n in names)
        positions=[(i,parse_pos(c)) for i,c in enumerate(cells)]
        positions=[(i,p) for i,p in positions if p is not None]
        if not positions: continue
        pos_i,pos=positions[0]
        date=""
        for c in cells:
            m=DATE_RE.search(c)
            if m:
                date=normalize_date(m.group(1)); break
        candidates=[]
        for i,c in enumerate(cells):
            if i==pos_i: continue
            if parse_pos(c) is not None: continue
            if DATE_RE.search(c): continue
            if len(c)>=4: candidates.append(c)
        race=max(candidates, key=len) if candidates else ""
        if not race: continue
        rec={"date":date,"race":race,"athlete":athlete.get("rider",""),"pos":pos,"src":source_id,"sourceUrl":url,"sourceId":source_id,"program":athlete.get("program",""),"discipline":athlete.get("primaryDiscipline",""),"team":athlete.get("team","")}
        if not date: rec["dateStatus"]="missing_from_source_parse"
        if contains_name or source_id in {"profileUrl","pcs_slug_guess","mtbdata_slug_guess"}:
            high.append(rec)
        else:
            rec["needsReview"]=True; rec["rawRow"]=joined; review.append(rec)
    return high, review


def scan_index_pages(roster: List[dict], sources: dict):
    high=[]; review=[]
    for s in sources.get("sources",[]):
        if not s.get("enabled", True): continue
        url=s.get("url"); sid=s.get("id") or s.get("name") or url
        if not url: continue
        html=fetch(url)
        if not html: continue
        lines=[ln.strip() for ln in soup_text(html).splitlines() if ln.strip()]
        context_race=""; context_date=""
        for line in lines:
            dm=DATE_RE.search(line)
            if dm:
                context_date=normalize_date(dm.group(1))
                context_race=re.sub(DATE_RE,"",line).strip(" -–|,") or context_race
            elif any(w in line.lower() for w in ["championship","world cup","classic","series","stage","results","race"]):
                context_race=line
            nline=norm(line)
            for a in roster:
                if not any(norm(n) and norm(n) in nline for n in athlete_names(a)): continue
                m=re.search(r"\b([1-9]|[12][0-9]|30)(?:st|nd|rd|th)?\b", line, re.I)
                if not m: continue
                rec={"date":context_date,"race":context_race,"athlete":a.get("rider",""),"pos":int(m.group(1)),"src":sid,"sourceUrl":url,"sourceId":sid,"rawLine":line,"program":a.get("program",""),"discipline":a.get("primaryDiscipline",""),"team":a.get("team","")}
                if not rec["date"]: rec["dateStatus"]="missing_from_source_parse"
                if rec["race"]: high.append(rec)
                else: rec["needsReview"]=True; review.append(rec)
    return high, review


def valid_record(r: dict) -> bool:
    try: pos=int(r.get("pos"))
    except Exception: return False
    return bool(r.get("race") and r.get("athlete") and 1 <= pos <= MAX_POSITION)


def main():
    roster_payload=load_json(ROSTER_FILE,{"athletes":[]})
    sources_payload=load_json(SOURCES_FILE,{"sources":[]})
    results_payload=load_json(RESULTS_FILE,{"results":[]})
    roster=roster_payload.get("athletes",[])
    roster_scan=roster[:MAX_ATHLETES_PER_RUN] if MAX_ATHLETES_PER_RUN else roster
    existing=results_payload.get("results",[])
    keys={result_key(r) for r in existing}
    high=[]; review=[]
    for a in roster_scan:
        for url,sid in build_profile_urls(a):
            html=fetch(url)
            if not html: continue
            h,r=parse_profile_tables(html,a,url,sid)
            high.extend(h); review.extend(r[:5])
    h,r=scan_index_pages(roster_scan,sources_payload)
    high.extend(h); review.extend(r[:5])
    additions=[]
    for rec in high:
        if not valid_record(rec): continue
        k=result_key(rec)
        if k not in keys:
            keys.add(k); additions.append(rec)
    combined=existing+additions
    combined.sort(key=lambda r:(str(r.get("date","")),str(r.get("race","")),str(r.get("athlete",""))))
    save_json(RESULTS_FILE,{"lastUpdated":datetime.now(timezone.utc).date().isoformat(),"generatedBy":"scripts/update_results.py","resultCount":len(combined),"addedThisRun":len(additions),"results":combined,"schema":{"date":"YYYY-MM-DD, may be blank when dateStatus is missing_from_source_parse","race":"string","athlete":"name matching roster rider or alias","pos":"number 1-30","src":"source label","sourceUrl":"optional evidence URL","sourceId":"optional source registry id","dateStatus":"optional missing_from_source_parse"}})
    save_json(REVIEW_FILE,{"lastUpdated":datetime.now(timezone.utc).isoformat(timespec="seconds"),"note":"Potential matches that were not confident enough to publish automatically.","candidateCount":len(review),"candidates":review})
    print(f"Roster athletes scanned: {len(roster_scan)}")
    print(f"Existing results: {len(existing)}")
    print(f"Publishable candidates: {len(high)}")
    print(f"Added this run: {len(additions)}")
    print(f"Review candidates: {len(review)}")
    print(f"Total results: {len(combined)}")

if __name__ == "__main__":
    main()
