#!/usr/bin/env python3
"""Champion Job Change Tracker.

Two modes, both on the Crustdata API (https://docs.crustdata.com):

  track     Re-check a list of known champions (profile URL + the company your CRM
            thinks they work at) with POST /person/enrich and flag everyone whose
            current employer changed.
  discover  Find people who held a champion-type title at your customer accounts,
            left recently and started somewhere new, with POST /person/search.

New moves are written to out/ and, optionally, posted to Slack.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

# Current "roles" that are not jobs: communities, boards, angel checks.
NOT_A_JOB = re.compile(r"\b(member|advisor|adviser|investor|angel|board|volunteer|mentor|ambassador|insider|alumni|fellow)\b", re.I)

PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.location",
    "social_handles.professional_network_identifier",
    "experience.employment_details",
]


def is_real_job(role: Dict[str, Any]) -> bool:
    return bool(role.get("title")) and not NOT_A_JOB.search(role.get("title", ""))


def role_domain(role: Dict[str, Any]) -> str:
    return normalize_domain(role.get("company_website") or role.get("company_website_domain"))


def same_company(role: Dict[str, Any], domain: str, name: str = "") -> bool:
    if domain and role_domain(role) == normalize_domain(domain):
        return True
    return bool(name) and (role.get("name") or "").strip().lower() == name.strip().lower()


def newest(roles: Iterable[Dict[str, Any]], key: str = "start_date") -> Optional[Dict[str, Any]]:
    roles = list(roles)
    if not roles:
        return None
    return max(roles, key=lambda r: (bool(r.get("is_default")), r.get(key) or ""))


def profile_url(profile: Dict[str, Any]) -> str:
    handles = profile.get("social_handles") or {}
    return ((handles.get("professional_network_identifier") or {}).get("profile_url")) or ""


def move_record(profile: Dict[str, Any], old: Dict[str, Any], new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "person_id": profile.get("crustdata_person_id"),
        "name": (profile.get("basic_profile") or {}).get("name", ""),
        "profile_url": profile_url(profile),
        "old_company": old.get("name", ""),
        "old_domain": normalize_domain(old.get("domain") or old.get("company_website")),
        "old_title": old.get("title", ""),
        "new_company": (new or {}).get("name", ""),
        "new_domain": role_domain(new or {}),
        "new_title": (new or {}).get("title", ""),
        "new_start": day((new or {}).get("start_date")),
        "new_company_headcount": (new or {}).get("company_headcount_range", ""),
        "new_company_industry": (new or {}).get("company_professional_network_industry", ""),
        "status": "moved" if new else "left_no_new_role",
    }


# ---------- track mode ----------

def detect_change(person_data: Dict[str, Any], crm_domain: str, crm_company: str = "",
                  crm_title: str = "") -> Optional[Dict[str, Any]]:
    """Return a move record if the person no longer works at the CRM company."""
    current = [r for r in (person_data.get("experience") or {}).get("employment_details", {}).get("current", []) or []
               if is_real_job(r)]
    if any(same_company(r, crm_domain, crm_company) for r in current):
        return None
    old = {"name": crm_company or crm_domain, "domain": crm_domain, "title": crm_title}
    return move_record(person_data, old, newest(current))


def track(client: Crustdata, champions: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    by_url = {c["profile_url"].rstrip("/"): c for c in champions if c.get("profile_url")}
    results = client.person_enrich(list(by_url), fields=["basic_profile", "social_handles", "experience.employment_details"])
    moves = []
    for row in results:
        if row.get("match_status") != "matched" or not row.get("matches"):
            continue
        champ = by_url.get((row.get("matched_on") or "").rstrip("/"))
        if not champ:
            continue
        person = row["matches"][0]["person_data"]
        person.setdefault("social_handles", {}).setdefault("professional_network_identifier", {})["profile_url"] = champ["profile_url"]
        move = detect_change(person, champ.get("company_domain", ""), champ.get("company_name", ""), champ.get("title", ""))
        if move:
            moves.append(move)
    return moves


# ---------- discover mode ----------

def title_matches(title: str, keywords: List[str]) -> bool:
    t = (title or "").lower()
    return any(k.lower() in t for k in keywords)


def find_move(profile: Dict[str, Any], domains: List[str], titles: List[str], since: str) -> Optional[Dict[str, Any]]:
    """A champion move: ended a matching role at a watched domain on/after `since`,
    is not still there, and started a real job elsewhere on/after `since`."""
    emp = (profile.get("experience") or {}).get("employment_details") or {}
    current = [r for r in emp.get("current") or [] if is_real_job(r)]
    watched = {normalize_domain(d) for d in domains}
    left = [r for r in emp.get("past") or []
            if role_domain(r) in watched and day(r.get("end_date")) >= since and title_matches(r.get("title", ""), titles)]
    if not left:
        return None
    old = newest(left, key="end_date")
    if any(role_domain(r) == role_domain(old) for r in current):
        return None  # internal move or still there
    new_roles = [r for r in current if day(r.get("start_date")) >= since and role_domain(r) not in watched]
    new = newest(new_roles)
    if not new:
        return None
    return move_record(profile, old, new)


def discover(client: Crustdata, domains: List[str], titles: List[str], since: str, max_results: int) -> List[Dict[str, Any]]:
    title_group = {"op": "or", "conditions": [
        {"field": "experience.employment_details.past.title", "type": "(.)", "value": t} for t in titles]}
    filters = {"op": "and", "conditions": [
        {"field": "experience.employment_details.past.company_website_domain", "type": "in", "value": domains},
        title_group,
        {"field": "experience.employment_details.current.start_date", "type": "=>", "value": since},
    ]}
    moves, seen = [], set()
    for profile in client.person_search(filters, PERSON_FIELDS, max_results=max_results):
        move = find_move(profile, domains, titles, since)
        if move and move["person_id"] not in seen:
            seen.add(move["person_id"])
            moves.append(move)
    return moves


# ---------- state, output ----------

def only_new(moves: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    return [m for m in moves if move_key(m) not in seen]


def move_key(m: Dict[str, Any]) -> str:
    return f"{m.get('person_id') or m.get('profile_url')}:{m.get('new_domain') or m.get('new_company')}"


def save_state(moves: List[Dict[str, Any]], state_path: str) -> None:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    seen.update(move_key(m) for m in moves)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def summary(moves: List[Dict[str, Any]]) -> Dict[str, Any]:
    moved = [m for m in moves if m["status"] == "moved"]
    return {
        "moves": len(moved),
        "left_without_new_role": len(moves) - len(moved),
        "from_accounts": dict(Counter(m["old_company"] for m in moves).most_common()),
        "new_company_size": dict(Counter(m["new_company_headcount"] or "unknown" for m in moved).most_common()),
    }


def slack_text(moves: List[Dict[str, Any]], limit: int = 15) -> str:
    lines = [f":rotating_light: {len(moves)} champion job change(s)"]
    for m in moves[:limit]:
        if m["status"] == "moved":
            lines.append(f"• *{m['name']}* ({m['old_title']} at {m['old_company']}) is now {m['new_title']} at "
                         f"*{m['new_company']}* since {m['new_start']} <{m['profile_url']}|profile>")
        else:
            lines.append(f"• *{m['name']}* left {m['old_company']}, no new role listed yet <{m['profile_url']}|profile>")
        if m.get("opener"):
            lines.append(f"   _Opener:_ {m['opener']}")
    if len(moves) > limit:
        lines.append(f"…and {len(moves) - limit} more in out/moves.csv")
    return "\n".join(lines)


def write_outputs(moves: List[Dict[str, Any]], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "moves.json"), "w") as fh:
        json.dump({"summary": summary(moves), "moves": moves}, fh, indent=1)
    cols = ["name", "profile_url", "old_company", "old_title", "new_company", "new_domain", "new_title",
            "new_start", "new_company_headcount", "status", "opener"]
    with open(os.path.join(out_dir, "moves.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(moves)


def draft_opener(move: Dict[str, Any]) -> Optional[str]:
    return ask_claude(
        "Write one short, warm first line (max 25 words) for a note to a former champion of our product. "
        f"They were {move['old_title']} at {move['old_company']} and just started as {move['new_title']} at "
        f"{move['new_company']}. Congratulate them, no hype, no emojis. Return only the line.")


def read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Champion Job Change Tracker (Crustdata data layer)")
    sub = p.add_subparsers(dest="mode", required=True)
    t = sub.add_parser("track", help="re-check known champions")
    t.add_argument("--champions", default="champions.csv", help="CSV: profile_url,company_domain[,company_name,title]")
    d = sub.add_parser("discover", help="find champion-type people who left your customer accounts")
    d.add_argument("--domains", default=os.environ.get("CUSTOMER_DOMAINS", ""), help="comma list, or a CSV path with a 'domain' column")
    d.add_argument("--titles", default=os.environ.get("CHAMPION_TITLES", "Revenue Operations,Sales Operations,Head of Sales,VP Sales"))
    d.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "90")))
    d.add_argument("--max-results", type=int, default=200)
    for sp in (t, d):
        sp.add_argument("--dry-run", action="store_true", help="never send to Slack")
        sp.add_argument("--all", action="store_true", help="include moves already alerted on")
        sp.add_argument("--draft-openers", action="store_true", help="draft a first line per move with Claude (needs ANTHROPIC_API_KEY)")
        sp.add_argument("--out", default="out")
        sp.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    client = Crustdata()
    if args.mode == "track":
        moves = track(client, read_csv(args.champions))
    else:
        domains = args.domains
        if domains.endswith(".csv"):
            domains = ",".join(r.get("domain", "") for r in read_csv(domains))
        domain_list = [normalize_domain(x) for x in domains.split(",") if x.strip()]
        if not domain_list:
            p.error("discover needs --domains (or CUSTOMER_DOMAINS in .env)")
        since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
        moves = discover(client, domain_list, [x.strip() for x in args.titles.split(",") if x.strip()], since, args.max_results)

    fresh = moves if args.all else only_new(moves, args.state)
    if args.draft_openers:
        if not claude_available():
            print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): skipping openers", file=sys.stderr)
        else:
            for m in fresh:
                if m["status"] == "moved":
                    m["opener"] = draft_opener(m) or ""
    write_outputs(fresh, args.out)
    print(json.dumps(summary(fresh), indent=1))
    if fresh:
        print(send_slack(slack_text(fresh), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(fresh, args.state)
    else:
        print("No new champion moves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
