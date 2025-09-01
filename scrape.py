import json, os, re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ----------------- helpers -----------------
def save_json(name, rows):
    Path(name).write_text(json.dumps(rows, indent=2, ensure_ascii=False))

def clean_text(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def get(url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    return requests.get(url, headers=headers, timeout=30, **kw)

# ----------------- Airbnb (Greenhouse) -----------------
def scrape_airbnb_greenhouse():
    url = "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true"
    r = get(url)
    r.raise_for_status()
    data = r.json()
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc = j.get("location", {}).get("name", "") if isinstance(j.get("location"), dict) else ""
        job_url = j.get("absolute_url") or j.get("url") or ""
        out.append({
            "source": "greenhouse",
            "company": "Airbnb",
            "title": clean_text(title),
            "location": clean_text(loc),
            "url": job_url
        })
    save_json("airbnb.json", out)

# ----------------- Liberty Mutual (iCIMS) -----------------
def scrape_liberty_icims():
    base = "https://careers-libertymutual.icims.com"
    out = []
    seen = set()

    # A) sitemap (fastest)
    try:
        sm = get(f"{base}/sitemap.xml")
        if sm.ok:
            links = re.findall(r"<loc>\s*(https://careers-libertymutual\.icims\.com/jobs/\d+/[^<]+)\s*</loc>", sm.text, flags=re.I)
            links = list(dict.fromkeys(links))[:200]
            for href in links:
                try:
                    h = get(href)
                    if not h.ok: continue
                    soup = BeautifulSoup(h.text, "lxml")
                    title = soup.select_one("h1")
                    title = clean_text(title.text if title else "")
                    if not title:
                        og = soup.select_one('meta[property="og:title"]')
                        if og: title = clean_text(og.get("content", ""))
                    loc = ""
                    loc_el = soup.select_one(".job-location") or soup.select_one("li.job-data-location span")
                    if loc_el: loc = clean_text(loc_el.text)
                    out.append({
                        "source": "icims",
                        "company": "Liberty Mutual",
                        "title": title or "(Job)",
                        "location": loc,
                        "url": href
                    })
                except Exception:
                    pass
    except Exception:
        pass

    # B) fallback: paginated search
    if not out:
        for pr in range(0, 6):
            list_url = f"{base}/jobs/search?ss=1&pr={pr}"
            h = get(list_url)
            if not h.ok: break
            links = re.findall(r"https://careers-libertymutual\.icims\.com/jobs/\d+/[^\s\"'>]+", h.text, flags=re.I)
            links = [l for l in links if l not in seen]
            if not links: break
            for href in links:
                seen.add(href)
                try:
                    d = get(href)
                    if not d.ok: continue
                    soup = BeautifulSoup(d.text, "lxml")
                    title = soup.select_one("h1")
                    title = clean_text(title.text if title else "")
                    loc = ""
                    loc_el = soup.select_one(".job-location") or soup.select_one("li.job-data-location span")
                    if loc_el: loc = clean_text(loc_el.text)
                    out.append({
                        "source": "icims",
                        "company": "Liberty Mutual",
                        "title": title or "(Job)",
                        "location": loc,
                        "url": href
                    })
                except Exception:
                    pass

    save_json("libertymutual.json", out)

# ----------------- Apple (Jobs via Playwright) -----------------
def scrape_apple(playwright_context, team_urls):
    out = []
    for team_url in team_urls:
        page = playwright_context.new_page()
        try:
            page.goto(team_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for client-rendered anchors to appear
            try:
                page.wait_for_selector("a[href*='details/']", timeout=15000)
            except Exception:
                pass
            # Collect detail links
            links = list(set(page.eval_on_selector_all(
                "a[href*='details/']",
                "els => els.map(a => a.href)"
            )))
            links = links[:80]  # cap to keep it quick
            for href in links:
                try:
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    title = ""
                    try:
                        title = page.locator("h1").first.text_content(timeout=5000) or ""
                    except Exception:
                        pass
                    title = clean_text(title)
                    location = ""
                    for sel in [".job-location", "li.location span"]:
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0:
                                txt = el.text_content(timeout=2000) or ""
                                location = clean_text(txt)
                                if location: break
                        except Exception:
                            pass
                    out.append({
                        "source": "apple",
                        "company": "Apple",
                        "title": title or "(Apple role)",
                        "location": location,
                        "url": href
                    })
                except Exception:
                    pass
        finally:
            try:
                page.close()
            except Exception:
                pass
    save_json("apple.json", out)

def scrape_zillow_workday(board_url: str, playwright_context):
    """
    Zillow (Workday) multi-tier:
      Tier 0: GET board_url with Accept: application/json,application/xml (direct JSON)
      Tier 1: CxS POST via requests (limit/offset)
      Tier 2: CxS POST via page.evaluate (same-origin)
      Tier 3: Iframe pagination, parse externalPath from HTML
    Writes zillow.json.
    """
    import re, json
    from urllib.parse import urlparse
    from pathlib import Path

    def clean(s): 
        return re.sub(r"\s+", " ", (s or "").strip())

    def build_detail(board_u: str, external_path: str):
        return board_u.rstrip("/") + "/job/" + external_path.lstrip("/")

    def normalize_from_postings(postings):
        rows = []
        for j in postings or []:
            ep = j.get("externalPath") or ""
            if not ep: 
                continue
            rows.append({
                "source": "workday",
                "company": "Zillow",
                "title": clean(j.get("title") or j.get("titleFacet") or "(Zillow role)"),
                "location": clean(j.get("locationsText") or ""),
                "url": build_detail(board_url, ep),
            })
        return rows

    def save(rows):
        Path("zillow.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    # ---------- Tier 0: GET board URL with Accept JSON/XML ----------
    try:
        hdr0 = {
            "User-Agent": UA,
            "Accept": "application/json,application/xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": board_url,
        }
        r0 = requests.get(board_url, headers=hdr0, timeout=40)
        if r0.status_code == 200:
            try:
                data0 = r0.json()
                # Some tenants nest in different keys; look for jobPostings anywhere
                text = json.dumps(data0)
                if '"jobPostings"' in text:
                    # Best effort: extract the array even if nested strangely
                    # Common layout: data0['body']['children'][…]['jobPostings'] – but varies.
                    # Use a recursive scan:
                    def iter_postings(obj):
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k == "jobPostings" and isinstance(v, list):
                                    yield v
                                else:
                                    yield from iter_postings(v)
                        elif isinstance(obj, list):
                            for it in obj:
                                yield from iter_postings(it)
                    all_batches = []
                    for batch in iter_postings(data0):
                        all_batches.extend(batch)
                    if all_batches:
                        rows = normalize_from_postings(all_batches)
                        if rows:
                            save(rows)
                            print(f"[Zillow Tier0] direct JSON: {len(rows)} jobs")
                            return rows
            except Exception:
                pass
        else:
            print(f"[Zillow Tier0] HTTP {r0.status_code}")
    except Exception as e:
        print(f"[Zillow Tier0] error: {e}")

    # ---------- Tier 1: CxS POST via requests ----------
    try:
        p = urlparse(board_url)
        host = p.netloc
        parts = [x for x in p.path.split("/") if x]
        board = parts[-1] if parts else "Zillow_Group_External"
        tenant = host.split(".")[0]
        cxs = f"https://{host}/wday/cxs/{tenant}/{board}/jobs"

        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": f"https://{host}",
            "Referer": board_url,
        }
        limit, offset, all_posts = 50, 0, []
        for _ in range(60):
            payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
            r = requests.post(cxs, headers=headers, json=payload, timeout=40)
            if r.status_code != 200:
                print(f"[Zillow CxS Tier1] HTTP {r.status_code} @offset {offset}")
                all_posts = []
                break
            data = r.json() or {}
            batch = data.get("jobPostings", []) or []
            all_posts.extend(batch)
            print(f"[Zillow CxS Tier1] got {len(batch)} @offset {offset} (total {len(all_posts)})")
            if len(batch) < limit:
                break
            offset += limit

        if all_posts:
            rows = normalize_from_postings(all_posts)
            if rows:
                save(rows)
                print(f"[Zillow CxS Tier1] final {len(rows)} jobs")
                return rows
    except Exception as e:
        print(f"[Zillow CxS Tier1] error: {e}")

    # ---------- Tier 2: in-page CxS via Playwright ----------
    page = playwright_context.new_page()
    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2500)
        except Exception:
            pass

        # Some tenants need an explicit Search click
        for sel in ("[data-automation-id='searchButton']", "button:has-text('Search')"):
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_enabled():
                try:
                    btn.click(timeout=2000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        page.wait_for_timeout(800)
                    break
                except Exception:
                    pass

        postings = []
        try:
            postings = page.evaluate(
                """
                async () => {
                  const parts = location.pathname.split('/').filter(Boolean);
                  const board = parts[parts.length - 1];
                  const tenant = location.host.split('.')[0];
                  const api = `/wday/cxs/${tenant}/${board}/jobs`;
                  const limit = 50;
                  let offset = 0, all = [];
                  for (let i = 0; i < 60; i++) {
                    const resp = await fetch(api, {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json;charset=UTF-8' },
                      body: JSON.stringify({ appliedFacets: {}, limit, offset, searchText: '' }),
                      credentials: 'same-origin'
                    });
                    if (!resp.ok) break;
                    const data = await resp.json();
                    const batch = (data && data.jobPostings) || [];
                    all = all.concat(batch);
                    if (batch.length < limit) break;
                    offset += limit;
                  }
                  return all;
                }
                """
            ) or []
        except Exception as e:
            print(f"[Zillow CxS Tier2] eval error: {e}")

        if postings:
            rows = normalize_from_postings(postings)
            if rows:
                save(rows)
                print(f"[Zillow CxS Tier2] final {len(rows)} jobs")
                return rows
    finally:
        try: page.close()
        except Exception: pass

    # ---------- Tier 3: iframe pagination; parse externalPath from inner HTML ----------
    page = playwright_context.new_page()
    out, seen = [], set()
    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2000)
        except Exception:
            pass

        # Pick the inner Workday frame
        def pick_workday_frame(pg):
            cands = [fr for fr in pg.frames if fr.url and ("myworkdayjobs" in fr.url or "workdayjobs" in fr.url)]
            for fr in cands:
                try:
                    if '"externalPath"' in fr.content():
                        return fr
                except Exception:
                    pass
            return cands[0] if cands else None

        ctx = pick_workday_frame(page)
        if ctx is None:
            print("[Zillow Tier3] ERROR: no Workday iframe found.")
            save([])
            return []

        # Click Search inside frame if present
        for sel in ("[data-automation-id='searchButton']", "button[aria-label='Search']", "button:has-text('Search')"):
            btn = ctx.locator(sel).first
            if btn.count() > 0 and btn.is_enabled():
                try:
                    btn.click(timeout=2000)
                    try:
                        ctx.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        ctx.wait_for_timeout(800)
                    break
                except Exception:
                    pass

        def parse_total_from_text(s):
            m = re.search(r"\bof\s+(\d+)\s+jobs?\b", s, flags=re.I)
            return int(m.group(1)) if m else None

        def collect_links_from_ctx():
            html = ctx.content()
            paths = re.findall(r'"externalPath"\s*:\s*"([^"]+)"', html)
            added = 0
            for p in paths:
                href = build_detail(board_url, p)
                if href not in seen:
                    seen.add(href)
                    added += 1
            return added

        def next_or_number(page_num):
            for sel in (
                "[data-automation-id='pagination-next']",
                "button[aria-label='Next Page']",
                "button[aria-label='Next']",
                "a[aria-label='Next']",
                "button:has-text('Next')",
                "//button[@data-uxi-element-id='next']",
            ):
                btn = ctx.locator(sel).first
                if btn.count() > 0 and btn.is_enabled():
                    try:
                        btn.click(timeout=2000)
                        try:
                            ctx.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            ctx.wait_for_timeout(800)
                        return True
                    except Exception:
                        pass
            num_sel = f"[data-automation-id='pagination-page'] button:has-text('{page_num}')"
            btn = ctx.locator(num_sel).first
            if btn.count() > 0 and btn.is_enabled():
                try:
                    btn.click(timeout=2000)
                    try:
                        ctx.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        ctx.wait_for_timeout(800)
                    return True
                except Exception:
                    pass
            return False

        # Page 1
        added = collect_links_from_ctx()
        try:
            inner_text = ctx.evaluate("() => document.body.innerText || ''")
        except Exception:
            inner_text = ""
        total_jobs = parse_total_from_text(inner_text)
        print(f"[Zillow Tier3] page 1: +{added} (total {len(seen)}/{total_jobs or '?'})")

        # Walk pages
        for pidx in range(2, 80):
            if not next_or_number(pidx):
                break
            before = len(seen)
            added = collect_links_from_ctx()
            gained = len(seen) - before
            # refresh total if needed
            if total_jobs is None:
                try:
                    inner_text = ctx.evaluate("() => document.body.innerText || ''")
                except Exception:
                    inner_text = ""
                total_jobs = parse_total_from_text(inner_text)
            print(f"[Zillow Tier3] page {pidx}: +{gained} (total {len(seen)}/{total_jobs or '?'})")
            if total_jobs and len(seen) >= total_jobs:
                break

        # Normalize minimal items
        rows = []
        for href in sorted(seen):
            m = re.search(r"/job/[^/]+/([^/?#]+)", href)
            title = ""
            if m:
                slug = m.group(1)
                title = clean(re.sub(r"[_-]+", " ", re.sub(r"\bR?\d{4,}\b", "", slug)))
            rows.append({
                "source": "workday",
                "company": "Zillow",
                "title": title or "(Zillow role)",
                "location": "",
                "url": href
            })
        save(rows)
        print(f"[Zillow Tier3] final total: {len(rows)} jobs")
        return rows

    finally:
        try: page.close()
        except Exception: pass

# ----------------- main -----------------
def main():
    import os
    from playwright.sync_api import sync_playwright

    # Optional overrides via repo Variables/Secrets
    apple_team_env = os.getenv("APPLE_TEAM_URLS", "")
    apple_team_urls = [u.strip() for u in apple_team_env.split(",") if u.strip()] or [
        "https://jobs.apple.com/en-us/search?team=software-quality-automation-and-tools-SFTWR-SQAT",
        "https://jobs.apple.com/en-us/search?team=quality-engineering-OPMFG-QE",
    ]
    zillow_board_url = os.getenv(
        "ZILLOW_BOARD_URL",
        "https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External"
    )

    print("Scraping Airbnb (Greenhouse)…")
    scrape_airbnb_greenhouse()

    print("Scraping Liberty Mutual (iCIMS)…")
    scrape_liberty_icims()

    print("Scraping Apple + Zillow…")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(user_agent=UA, locale="en-US")

        # Apple (Playwright)
        scrape_apple(context, apple_team_urls)

        # Zillow (robust: CxS + shadow-aware pagination inside this function)
        scrape_zillow_workday(zillow_board_url, context)

        context.close()
        browser.close()

    print("Done. Wrote airbnb.json, libertymutual.json, apple.json, zillow.json")


if __name__ == "__main__":
    main()

