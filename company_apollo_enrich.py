"""
Company Apollo enrichment — CSV in, CSV out.

Input: any CSV with company name + Website + optional Email (separate columns).
Output: all input columns + person name, person title, person email, person phone, person id.

Optional phone reveal: APOLLOCOMPANY_REVEAL_PHONE_NUMBER=true sends reveal_phone_number + webhook_url
to people/match; person phone stays null until your webhook callback fills it later.

Domain priority: Website column first; Email only when Website is empty/missing.
Set APOLLOCOMPANY_COLUMN_EMAIL empty (or omit) to disable email fallback entirely.

Flow (same as amazon_seller_pipeline Stage 5):
  resolve domain → Apollo org → list people → GPT rank up to 3 → first verified email match.
  Org search tiers: domain → name+HQ location (city/state) → name-only (when fallback enabled).

Parallel: one Apollo+GPT chain per unique company (domain or name); 4 workers by default.
"""

import csv
import html
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from dotenv import load_dotenv
from openai import OpenAI

from config_env import override_from_env

CONFIG = {
    "INPUT_CSV": "data/companiesApollo.csv",
    "OUTPUT_CSV": "data/company_leads_apollo.csv",
    "COLUMN_COMPANY_NAME": ["Company Name", "company name", "Seller Name"],
    "COLUMN_WEBSITE": ["Website", "website"],
    "COLUMN_EMAIL": [],
    "COLUMN_CITY": ["Sitz", "City", "city"],
    "COLUMN_STATE": ["Bundesland", "State", "state"],
    "COLUMN_COUNTRY": [],
    "WRITE_BATCH_SIZE": 50,
    "WAIT_BETWEEN_REQUESTS": 1,
    "APOLLO_BASE_URL": "https://api.apollo.io/api/v1",
    "APOLLO_API_TIMEOUT": 60,
    "APOLLO_HTTP_RETRIES": 3,
    "APOLLO_RATE_LIMIT_PER_MIN": 180,
    "WORKERS": 4,
    "APOLLO_SEARCH_BY_NAME_FALLBACK": False,
    "SKIP_EXISTING": True,
    "REVEAL_PHONE_NUMBER": False,
    "WEBHOOK_URL": "",
    "MODEL": "gpt-5-mini",
    "REASONING_EFFORT": "medium",
    "MAX_OUTPUT_TOKENS": 5000,
}

OUTPUT_EXTRA_HEADERS = [
    "person name",
    "person title",
    "person email",
    "person phone",
    "person id",
]

APOLLO_API_KEY = ""
PUBLIC_EMAIL_DOMAINS = frozenset()
client = None

_COMPANY_LEGAL_SUFFIXES = [
    "GmbH & Co. KG",
    "GmbH & Co.KG",
    "GmbH & Co KG",
    "GmbH & Co. KGaA",
    "AG & Co. KG",
    "UG (haftungsbeschränkt)",
    "UG (haftungsbeschrankt)",
    "eingetragene Genossenschaft",
    "e.V.",
    "e. V.",
    "e.K.",
    "e.Kfm.",
    "e.Kfr.",
    "GmbH",
    "UG",
    "AG",
    "KG",
    "OHG",
    "GbR",
    "PartG",
    "PartG mbB",
    "SE",
    "mbH",
    "S.A.",
    "S.A.R.L.",
    "S.à r.l.",
    "SARL",
    "Sàrl",
    "BV",
    "B.V.",
    "NV",
    "PLC",
    "Ltd.",
    "Ltd",
    "Limited",
    "LLC",
    "Inc.",
    "Inc",
    "Corp.",
    "Corp",
    "Co.",
    "Co",
    "LP",
    "LLP",
    "Pty Ltd",
    "Pty. Ltd.",
    "d.o.o.",
    "d.o.o",
    "s.r.o.",
    "s.r.o",
    "Sp. z o.o.",
    "Sp. z o.o",
    "S.p.A.",
    "SpA",
    "Srl",
    "S.r.l.",
    "A/S",
    "ApS",
    "AB",
    "Oy",
    "Oyj",
    "ASA",
    "AS",
    "IKS",
    "EE",
    "OÜ",
    "UAB",
    "Kft.",
    "Kft",
    "Zrt.",
    "Zrt",
    "Rt.",
    "Rt",
    "Bt.",
    "Bt",
    "KH",
    "A.H.",
    "AH",
    "Händler",
    "Handels GmbH",
    "Handelsgesellschaft",
    "Einzelunternehmen",
]

SYSTEM_PROMPT_STAGE5 = """You help choose the best internal contacts at a company for a B2B outreach scenario.

Context:
- We are a visual / creative studio helping Amazon sellers improve product visuals, A+ content, infographics, listing images, and conversion-focused creative.
- We want to email the most relevant people who likely influence Amazon listing visuals, brand content, e-commerce merchandising, performance marketing creative, or marketplace operations.

Input:
- You receive numbered rows. Each row lists Apollo person id and job title (from Apollo only). Titles may be in German or English.

Rules:
- Pick at most 3 people, ranked best → second → third. Only include people whose titles clearly relate to Amazon/marketplace, e-commerce, performance or growth, marketing, brand, content, creative, design, graphics, product, merchandising, or similar. Skip unrelated roles (pure accounting, HR, IT infrastructure, legal-only, etc.).
- If fewer than 3 are clearly suitable, return only those (1 or 2). If none are suitable, respond with exactly one line: null
- Use ONLY the provided ids and titles. Do not invent ids or titles.
- Output format (no markdown, no extra text):
  Line 1: <apollo_person_id>, <title exactly as provided or lightly trimmed whitespace>
  Line 2: optional second
  Line 3: optional third
- If nothing suitable: a single line containing only null
"""


def config_bool(key: str, default: bool = False) -> bool:
    v = CONFIG.get(key, default)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("0", "false", "no", "off", "none", ""):
        return False
    if s in ("1", "true", "yes", "on"):
        return True
    return default


def load_public_email_domains():
    global PUBLIC_EMAIL_DOMAINS
    p = Path(__file__).resolve().parent / "public_email_domains.txt"
    domains = set()
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                domains.add(line)
    domains.update(
        {
            "gmail.com",
            "googlemail.com",
            "yahoo.com",
            "yahoo.de",
            "hotmail.com",
            "hotmail.de",
            "outlook.com",
            "gmx.de",
            "web.de",
            "t-online.de",
        }
    )
    PUBLIC_EMAIL_DOMAINS = frozenset(domains)


def extract_clean_company_name(name: str) -> str:
    if not name or str(name).strip().lower() in ("", "null"):
        return ""
    s = html.unescape(str(name)).strip()
    s = re.sub(r"\s+", " ", s)
    for _ in range(12):
        changed = False
        for suf in _COMPANY_LEGAL_SUFFIXES:
            pat = re.compile(rf"(?i)[\s,./\-\(\[\u2013\u2014&]*{re.escape(suf)}\s*$")
            ns, n = pat.subn("", s)
            if n:
                s = ns.strip(" ,.-–—/&()[]")
                changed = True
                break
        if not changed:
            break
    s = re.sub(r"\s+", " ", s).strip(" ,.-–—/&")
    return s


def extract_company_domain_from_email(email: str) -> Optional[str]:
    if not email or str(email).strip().lower() in ("", "null"):
        return None
    em = str(email).strip().strip("'").strip('"')
    if "@" not in em:
        return None
    dom = em.split("@", 1)[1].strip().lower()
    if not dom or dom in PUBLIC_EMAIL_DOMAINS:
        return None
    dom = dom.split("/")[0].split("?")[0].strip().rstrip(">")
    if dom in PUBLIC_EMAIL_DOMAINS:
        return None
    return dom or None


def extract_domain_from_website(raw: str) -> Optional[str]:
    if not raw or str(raw).strip().lower() in ("", "null"):
        return None
    s = str(raw).strip().strip("'").strip('"')
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        s = "https://" + s
    try:
        parsed = urlparse(s)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host or host in PUBLIC_EMAIL_DOMAINS:
        return None
    return host


def normalize_column_aliases(key: str) -> List[str]:
    """Return header aliases; empty list when env unset or blank (disables email column)."""
    v = CONFIG.get(key)
    if v is None:
        return []
    if isinstance(v, list):
        return [a.strip() for a in v if a and str(a).strip()]
    s = str(v).strip()
    if not s or s.lower() in ("none", "null"):
        return []
    return [c.strip() for c in s.split(",") if c.strip()]


def email_column_enabled() -> bool:
    return bool(normalize_column_aliases("COLUMN_EMAIL"))


def resolve_company_domain(website: str, email: str) -> Tuple[Optional[str], str]:
    """
    Website wins when the row has a non-empty website value (no email fallback then).
    Email domain only when website value is empty and APOLLOCOMPANY_COLUMN_EMAIL is configured.
    Returns (domain, source) where source is website | email | none.
    """
    w = (website or "").strip()
    if w and w.lower() != "null":
        return extract_domain_from_website(w), "website"
    if email_column_enabled():
        e = (email or "").strip()
        if e and e.lower() != "null":
            return extract_company_domain_from_email(e), "email"
    return None, "none"


def pick_column(row: Dict[str, str], aliases: List[str]) -> str:
    if not aliases:
        return ""
    lower_map = {k.strip().lower(): k for k in row.keys() if k}
    for alias in aliases:
        key = lower_map.get(alias.strip().lower())
        if key is not None:
            return (row.get(key) or "").strip()
    return ""


def null_str(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, str) and not v.strip():
        return "null"
    return str(v)


_apollo_rate_lock = threading.Lock()
_apollo_rate_timestamps: deque = deque()


def _apollo_acquire_rate_slot():
    limit = int(CONFIG.get("APOLLO_RATE_LIMIT_PER_MIN", 180))
    window = 60.0
    with _apollo_rate_lock:
        now = time.time()
        while _apollo_rate_timestamps and now - _apollo_rate_timestamps[0] >= window:
            _apollo_rate_timestamps.popleft()
        if len(_apollo_rate_timestamps) >= limit:
            sleep_for = window - (now - _apollo_rate_timestamps[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.time()
            while _apollo_rate_timestamps and now - _apollo_rate_timestamps[0] >= window:
                _apollo_rate_timestamps.popleft()
        _apollo_rate_timestamps.append(time.time())


def _retry_after_seconds(response, attempt: int) -> float:
    retry_after = (response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return max(float(retry_after), 0.5)
        except ValueError:
            pass
    return min(2 ** attempt, 30)


def apollo_post(path, json_body=None, params=None):
    url = (CONFIG.get("APOLLO_BASE_URL") or "https://api.apollo.io/api/v1").rstrip("/") + path
    headers = {
        "x-api-key": APOLLO_API_KEY,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    max_retries = int(CONFIG.get("APOLLO_HTTP_RETRIES", 3))
    last_err = None
    for attempt in range(1, max_retries + 1):
        _apollo_acquire_rate_slot()
        try:
            r = requests.post(
                url,
                headers=headers,
                json=json_body if json_body is not None else {},
                params=params,
                timeout=int(CONFIG.get("APOLLO_API_TIMEOUT", 60)),
            )
            if r.status_code == 429:
                wait = _retry_after_seconds(r, attempt)
                print(f"      ⚠️  Apollo 429 rate limit; waiting {wait:.1f}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            last_err = e
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 429:
                wait = _retry_after_seconds(resp, attempt)
                print(f"      ⚠️  Apollo 429 rate limit; waiting {wait:.1f}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"      ⚠️  Apollo HTTP attempt {attempt}/{max_retries}: {e}")
            time.sleep(min(2 ** attempt, 30))
        except Exception as e:
            last_err = e
            print(f"      ⚠️  Apollo HTTP attempt {attempt}/{max_retries}: {e}")
            time.sleep(min(2 ** attempt, 30))
    print(f"      ❌ Apollo failed after retries: {last_err}")
    return None


def _first_apollo_org(data: Optional[dict]) -> Optional[dict]:
    if not data or not isinstance(data, dict):
        return None
    orgs = data.get("organizations") or []
    if orgs:
        return orgs[0]
    accounts = data.get("accounts") or []
    if accounts:
        first = accounts[0] if isinstance(accounts[0], dict) else {}
        oid = (first.get("organization_id") or "").strip()
        if oid:
            return {"id": oid, "_from_account": True}
    return None


def build_organization_locations(city: str, state: str, country: str) -> List[str]:
    """Build deduped Apollo organization_locations from row fields (city, state, country)."""
    out: List[str] = []
    seen: set = set()
    for raw in (city, state, country):
        v = (raw or "").strip()
        if not v or v.lower() == "null":
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _apollo_search_by_domain(domain: str) -> Optional[dict]:
    path = "/mixed_companies/search"
    data = apollo_post(path, json_body={"q_organization_domains_list": [domain]})
    org = _first_apollo_org(data)
    if org:
        print(f"      Domain search: {domain} → found org {org.get('id', '')}")
    else:
        print(f"      Domain search: {domain} → 0 results")
    return org


def _apollo_search_by_name(company_name: str, locations: Optional[List[str]] = None) -> Optional[dict]:
    path = "/mixed_companies/search"
    qn = (company_name or "").strip()
    if not qn:
        return None
    body: Dict[str, Any] = {"q_organization_name": qn, "per_page": 10}
    locs = [x for x in (locations or []) if x and str(x).strip()]
    if locs:
        body["organization_locations"] = locs
        print(f"      Name+location search: {qn} | locations={locs}")
    else:
        print(f"      Name-only search: {qn} (no location columns)")
    data = apollo_post(path, json_body=body)
    org = _first_apollo_org(data)
    if org:
        tier = "name+location" if locs else "name-only"
        print(f"      {tier} search → found org {org.get('id', '')}")
    else:
        tier = "name+location" if locs else "name-only"
        print(f"      {tier} search → 0 results")
    return org


def apollo_search_company(
    domain: Optional[str],
    company_name: str,
    locations: Optional[List[str]] = None,
) -> Optional[dict]:
    name_fallback = config_bool("APOLLO_SEARCH_BY_NAME_FALLBACK")
    qn = (company_name or "").strip()
    locs = [x for x in (locations or []) if x and str(x).strip()]

    if domain:
        org = _apollo_search_by_domain(domain)
        if org:
            return org
        if not name_fallback:
            return None
        if not qn:
            return None
        print("      Domain miss; trying name fallback")
        if locs:
            return _apollo_search_by_name(qn, locs)
        return _apollo_search_by_name(qn, None)

    if not name_fallback:
        return None
    if not qn:
        return None
    if locs:
        return _apollo_search_by_name(qn, locs)
    return _apollo_search_by_name(qn, None)


def apollo_list_people(org_id: str):
    data = apollo_post(
        "/mixed_people/api_search",
        json_body={"organization_ids": [org_id], "per_page": 100},
    )
    if not data:
        return []
    return data.get("people") or []


def apollo_match_person(person_id: str):
    body = {"id": person_id}
    if config_bool("REVEAL_PHONE_NUMBER"):
        body["reveal_phone_number"] = True
        body["webhook_url"] = (CONFIG.get("WEBHOOK_URL") or "").strip()
    data = apollo_post("/people/match", json_body=body)
    if not data:
        return None
    return data.get("person") or data


def call_gpt_stage5_rank_people(company_name: str, people_lines: List[Tuple[str, str]]) -> str:
    lines = [
        "Company context name (for orientation only, do not invent contacts): " + (company_name or "unknown"),
        "Pick up to 3 best Apollo contacts for our Amazon visual/creative outreach (see system rules).",
        "Nr | apollo_person_id | title",
        "-" * 80,
    ]
    for i, (pid, title) in enumerate(people_lines, 1):
        lines.append(f"{i} | {pid} | {title}")
    lines.append("-" * 80)
    lines.append("Answer with up to 3 lines: id, title per line. Or single line: null")
    user_prompt = "\n".join(lines)
    try:
        response = client.responses.create(
            model=CONFIG["MODEL"],
            reasoning={"effort": CONFIG["REASONING_EFFORT"]},
            input=[
                {"role": "system", "content": SYSTEM_PROMPT_STAGE5},
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=int(CONFIG["MAX_OUTPUT_TOKENS"]),
        )
        return (response.output_text or "").strip()
    except Exception as e:
        print(f"      GPT error: {e}")
        return ""


def parse_stage5_gpt_people(text: str) -> List[Tuple[str, str]]:
    if not text or text.strip().lower() == "null":
        return []
    out = []
    for ln in text.splitlines():
        s = ln.strip().strip("`")
        if not s or s.lower() == "null":
            continue
        if "," not in s:
            continue
        pid, title = s.split(",", 1)
        pid = pid.strip()
        title = title.strip()
        if pid and pid.lower() != "null":
            out.append((pid, title))
        if len(out) >= 3:
            break
    return out


def pick_verified_apollo_contact(ordered_ids: List[str]):
    """Try people/match in order; return (name, title, email, phone, person_id) or None."""
    reveal_phone = config_bool("REVEAL_PHONE_NUMBER")
    for pid in ordered_ids:
        if not pid:
            continue
        person = apollo_match_person(pid)
        time.sleep(float(CONFIG.get("WAIT_BETWEEN_REQUESTS", 1)))
        if not person or not isinstance(person, dict):
            continue
        email = (person.get("email") or "").strip()
        status = str(person.get("email_status") or "").strip().lower()
        if email and status == "verified":
            name = (person.get("name") or "").strip()
            if not name:
                fn = (person.get("first_name") or "").strip()
                ln = (person.get("last_name") or "").strip()
                name = (fn + " " + ln).strip()
            title = (person.get("title") or "").strip()
            person_id = (person.get("id") or pid).strip()
            phone = "null"
            if reveal_phone:
                print(f"      person id={person_id} | phone pending webhook")
            return name or "null", title or "null", email, phone, null_str(person_id)
    return None


def cache_key(domain: Optional[str], company_name: str) -> str:
    if domain:
        return f"domain:{domain.lower()}"
    n = (company_name or "").strip().lower()
    return f"name:{n}" if n else "name:unknown"


def enrich_company(
    domain: Optional[str],
    company_name: str,
    locations: Optional[List[str]] = None,
) -> Tuple[str, str, str, str, str]:
    """Returns person name, title, email, phone, person id (null strings on failure)."""
    null5 = ("null", "null", "null", "null", "null")
    if not domain and not config_bool("APOLLO_SEARCH_BY_NAME_FALLBACK"):
        print("   Skip: no usable domain (name-search disabled)")
        return null5
    if not domain and not (company_name or "").strip():
        print("   Skip: no domain and no company name")
        return null5

    org = apollo_search_company(domain, company_name, locations)
    time.sleep(float(CONFIG.get("WAIT_BETWEEN_REQUESTS", 1)))
    if not org or not isinstance(org, dict) or not org.get("id"):
        print("   Skip: no Apollo organization")
        return null5

    people = apollo_list_people(org["id"])
    time.sleep(float(CONFIG.get("WAIT_BETWEEN_REQUESTS", 1)))
    if not people:
        print("   Skip: no people on organization")
        return null5

    people_lines: List[Tuple[str, str]] = []
    for p in people:
        if not isinstance(p, dict):
            continue
        pid = (p.get("id") or "").strip()
        tit = (p.get("title") or "").strip()
        if pid and tit:
            people_lines.append((pid, tit))
    if not people_lines:
        print("   Skip: no id/title pairs")
        return null5

    gpt_raw = call_gpt_stage5_rank_people(company_name, people_lines)
    picks = parse_stage5_gpt_people(gpt_raw)
    if not picks:
        print("   Skip: GPT found no suitable contacts")
        return null5

    ordered_ids = [p[0] for p in picks]
    matched = pick_verified_apollo_contact(ordered_ids)
    if not matched:
        print("   Skip: no verified Apollo email on top picks")
        return null5

    name, title, email, phone, person_id = matched
    print(f"   Matched: {name} | {title} | {email} | id={person_id}")
    return null_str(name), null_str(title), null_str(email), phone, person_id


def row_identity_key(row: Dict[str, str]) -> Tuple[str, str, str]:
    name = pick_column(row, normalize_column_aliases("COLUMN_COMPANY_NAME")).lower()
    web = pick_column(row, normalize_column_aliases("COLUMN_WEBSITE")).lower()
    em = pick_column(row, normalize_column_aliases("COLUMN_EMAIL")).lower()
    return name, web, em


def load_done_row_keys(path: Path) -> set:
    """Input rows already enriched (person email set and not null)."""
    done = set()
    if not path.is_file():
        return done
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            em = (row.get("person email") or "").strip().lower()
            if em and em != "null":
                done.add(row_identity_key(row))
    return done


def build_row_context(row: Dict[str, str]) -> Dict[str, Any]:
    raw_name = pick_column(row, CONFIG["COLUMN_COMPANY_NAME"])
    website = pick_column(row, CONFIG["COLUMN_WEBSITE"])
    email = pick_column(row, CONFIG["COLUMN_EMAIL"]) if email_column_enabled() else ""
    city = pick_column(row, CONFIG["COLUMN_CITY"])
    state = pick_column(row, CONFIG["COLUMN_STATE"])
    country = pick_column(row, CONFIG["COLUMN_COUNTRY"])
    clean_name = extract_clean_company_name(raw_name)
    if not clean_name and raw_name and raw_name.lower() != "null":
        clean_name = re.sub(r"\s+", " ", html.unescape(raw_name)).strip()
    domain, domain_src = resolve_company_domain(website, email)
    ctx_name = clean_name or raw_name or "unknown"
    locations = build_organization_locations(city, state, country)
    return {
        "raw_name": raw_name,
        "domain": domain,
        "domain_src": domain_src,
        "ctx_name": ctx_name,
        "locations": locations,
        "key": cache_key(domain, ctx_name),
    }


def _process_company_work(key: str, ctx: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    domain = ctx.get("domain")
    ctx_name = ctx.get("ctx_name") or "unknown"
    locations = ctx.get("locations") or []
    loc_hint = f" | locations={locations}" if locations else ""
    print(
        f"\n   [company {key}] name={ctx_name} | domain={domain or 'none'} | "
        f"source={ctx.get('domain_src')}{loc_hint}"
    )
    return enrich_company(domain, ctx_name, locations)


def run_company_worker_pool(
    work_items: List[Tuple[str, Dict[str, Any]]],
    max_workers: int,
) -> Dict[str, Tuple[str, str, str, str, str]]:
    results: Dict[str, Tuple[str, str, str, str, str]] = {}
    workers = max(1, int(max_workers))
    null5 = ("null", "null", "null", "null", "null")
    print(f"   Parallel enrich: {len(work_items)} unique companies, {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_key = {
            executor.submit(_process_company_work, key, ctx): key for key, ctx in work_items
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
                print(f"   ✓ Completed company {key}")
            except Exception as e:
                print(f"      ❌ Worker error for {key}: {e}")
                results[key] = null5
    return results


def flush_rows(path: str, headers: List[str], rows: List[Dict[str, str]], mode: str):
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


def run():
    global APOLLO_API_KEY, client

    load_dotenv(Path(__file__).resolve().parent / ".env")
    override_from_env(CONFIG, env_prefix="APOLLOCOMPANY_")

    CONFIG["COLUMN_WEBSITE"] = normalize_column_aliases("COLUMN_WEBSITE") or ["Website", "website"]
    CONFIG["COLUMN_EMAIL"] = normalize_column_aliases("COLUMN_EMAIL")
    CONFIG["COLUMN_COMPANY_NAME"] = normalize_column_aliases("COLUMN_COMPANY_NAME") or [
        "Company Name",
        "company name",
        "Seller Name",
    ]
    CONFIG["COLUMN_CITY"] = normalize_column_aliases("COLUMN_CITY") or ["Sitz", "City", "city"]
    CONFIG["COLUMN_STATE"] = normalize_column_aliases("COLUMN_STATE") or [
        "Bundesland",
        "State",
        "state",
    ]
    CONFIG["COLUMN_COUNTRY"] = normalize_column_aliases("COLUMN_COUNTRY")

    if config_bool("REVEAL_PHONE_NUMBER"):
        webhook = (CONFIG.get("WEBHOOK_URL") or "").strip()
        if not webhook:
            raise ValueError(
                "APOLLOCOMPANY_WEBHOOK_URL is required when APOLLOCOMPANY_REVEAL_PHONE_NUMBER=true"
            )

    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    APOLLO_API_KEY = (os.getenv("APOLLO_API_KEY") or "").strip()
    if not openai_key:
        raise ValueError("OPENAI_API_KEY not found in .env")
    if not APOLLO_API_KEY:
        raise ValueError("APOLLO_API_KEY not found in .env")

    load_public_email_domains()
    client = OpenAI(api_key=openai_key)

    in_path = Path(CONFIG["INPUT_CSV"])
    out_path = Path(CONFIG["OUTPUT_CSV"])
    if not in_path.is_file():
        raise FileNotFoundError(f"Input CSV not found: {in_path}")

    skip_existing = config_bool("SKIP_EXISTING", True)
    done_row_keys = load_done_row_keys(out_path) if skip_existing and out_path.is_file() else set()

    print("\n" + "=" * 90)
    print("COMPANY APOLLO ENRICH")
    print("=" * 90)
    print(f"Input:  {in_path}")
    print(f"Output: {out_path}")
    print(f"Model:  {CONFIG['MODEL']}")
    print(f"Name fallback: {config_bool('APOLLO_SEARCH_BY_NAME_FALLBACK')}")
    print(f"Email column: {'enabled' if email_column_enabled() else 'disabled'}")
    if config_bool("REVEAL_PHONE_NUMBER"):
        print(f"Phone reveal: enabled (webhook → {CONFIG.get('WEBHOOK_URL', '')})")
    else:
        print("Phone reveal: disabled")
    print(f"Skip existing: {skip_existing} ({len(done_row_keys)} rows already done)")
    print(f"Workers: {CONFIG.get('WORKERS', 4)}")

    with open(in_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        input_headers = list(reader.fieldnames or [])
        all_rows = list(reader)

    out_headers = input_headers + [h for h in OUTPUT_EXTRA_HEADERS if h not in input_headers]

    pending: List[Tuple[int, Dict[str, str], Dict[str, Any]]] = []
    skipped_resume = 0
    for idx, row in enumerate(all_rows, 1):
        if skip_existing and row_identity_key(row) in done_row_keys:
            skipped_resume += 1
            print(f"\n[row {idx}] skipped (already in output)")
            continue
        pending.append((idx, row, build_row_context(row)))

    company_reps: Dict[str, Dict[str, Any]] = {}
    row_keys: List[str] = []
    for _idx, _row, ctx in pending:
        key = ctx["key"]
        row_keys.append(key)
        if key not in company_reps:
            company_reps[key] = ctx

    company_cache = run_company_worker_pool(
        list(company_reps.items()),
        CONFIG.get("WORKERS", 4),
    )
    null5 = ("null", "null", "null", "null", "null")
    for key in company_reps:
        if key not in company_cache:
            company_cache[key] = null5

    mode = "a" if out_path.is_file() and skip_existing else "w"
    buf: List[Dict[str, str]] = []
    written = 0
    cache_hits = 0
    seen_company_keys: set = set()
    batch_size = int(CONFIG.get("WRITE_BATCH_SIZE", 50))

    for (idx, row, _ctx), key in zip(pending, row_keys):
        pname, ptitle, pemail, pphone, pid = company_cache.get(key, null5)
        if key in seen_company_keys:
            cache_hits += 1
        else:
            seen_company_keys.add(key)
        print(f"\n[row {idx}] applying result for {key}")
        out = dict(row)
        out["person name"] = pname
        out["person title"] = ptitle
        out["person email"] = pemail
        out["person phone"] = pphone
        out["person id"] = pid
        buf.append(out)

        if len(buf) >= batch_size:
            flush_rows(str(out_path), out_headers, buf, mode)
            written += len(buf)
            print(f"   Flushed {len(buf)} rows (total: {written})")
            buf = []
            mode = "a"

    if buf:
        flush_rows(str(out_path), out_headers, buf, mode)
        written += len(buf)
        print(f"   Final flush {len(buf)} rows (total: {written})")

    print(
        f"\nDone. Written: {written} | Resume skipped: {skipped_resume} | "
        f"Unique companies: {len(company_reps)} | Cache hits: {cache_hits}"
    )


if __name__ == "__main__":
    run()
