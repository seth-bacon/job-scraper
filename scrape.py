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
    Zillow (Workday) — same-origin JSON first, UI fallback second.
      1) Click Search if needed
      2) In-page fetch to multiple CxS endpoints (limit/offset pagination)
      3) If JSON fails, walk Next/numeric pages and parse "externalPath" from HTML
      4) Save zillow.json
    """
    import re, json
    from pathlib import Path
    from urllib.parse import urlparse

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

    page = playwright_context.new_page()
    out_rows, seen_urls = [], set()

    def click_search_if_needed(pg):
        for sel in ("[data-automation-id='searchButton']", "button[aria-label='Search']", "button:has-text('Search')"):
            btn = pg.locator(sel).first
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click(timeout=2500)
                    try:
                        pg.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pg.wait_for_timeout(800)
                    return True
            except Exception:
                pass
        return False

    def parse_total_from_text(txt: str):
        m = re.search(r"\bof\s+(\d+)\s+jobs?\b", txt, flags=re.I)
        return int(m.group(1)) if m else None

    def collect_external_paths_from_html(html: str):
        return re.findall(r'"externalPath"\s*:\s*"([^"]+)"', html)

    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)

        # Cookie banner (outer page)
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2000)
        except Exception:
            pass

        clicked_search = click_search_if_needed(page)

        # ---------- Tier A: in-page CxS JSON (multiple endpoints) ----------
        try:
            result = page.evaluate(
                """
                async () => {
                  const parts = location.pathname.split('/').filter(Boolean);
                  const board = parts[parts.length - 1];
                  const tenant = location.host.split('.')[0];

                  const endpoints = [
                    `/wday/cxs/${tenant}/${board}/jobs`,
                    `/wday/cxs/${tenant}/${board}/search/jobs`,
                    `/wday/cxs/careers/${board}/jobs`,
                  ];
                  const headers = {
                    'Content-Type': 'application/json;charset=UTF-8',
                    'Accept': 'application/json,application/xml'
                  };
                  const limit = 50;
                  for (const api of endpoints) {
                    let all = [];
                    for (let offset = 0; offset < 3000; offset += limit) {
                      const resp = await fetch(api, {
                        method: 'POST',
                        headers,
                        body: JSON.stringify({ appliedFacets: {}, limit, offset, searchText: '' }),
                        credentials: 'same-origin'
                      });
                      if (!resp.ok) { all = []; break; }
                      const data = await resp.json();
                      const batch = (data && (data.jobPostings || data.items || [])) || [];
                      all = all.concat(batch);
                      if (batch.length < limit) break;
                    }
                    if (all.length) return { api, postings: all };
                  }
                  return { api: null, postings: [] };
                }
                """
            )
        except Exception as e:
            print(f"[Zillow JSON] eval error: {e}")
            result = {"api": None, "postings": []}

        postings = result.get("postings") or []
        api_used = result.get("api")

        if postings:
            rows = normalize_from_postings(postings)
            Path("zillow.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
            print(f"[Zillow JSON] {len(rows)} jobs via {api_used}")
            return rows

        # ---------- Tier B: UI pagination + HTML parse (externalPath) ----------
        # Try to read total label (optional)
        try:
            txt = page.evaluate("() => document.body.innerText || ''")
        except Exception:
            txt = ""
        total_jobs = parse_total_from_text(txt)

        def click_next_or_number(pg, page_number):
            for sel in (
                "[data-automation-id='pagination-next']",
                "button[aria-label='Next Page']",
                "button[aria-label='Next']",
                "a[aria-label='Next']",
                "button:has-text('Next')",
                "//button[@data-uxi-element-id='next']",
            ):
                btn = pg.locator(sel).first
                try:
                    if btn.count() > 0 and btn.is_enabled():
                        btn.click(timeout=2500)
                        try:
                            pg.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pg.wait_for_timeout(800)
                        return True
                except Exception:
                    pass
            # numeric page button
            num_sel = f"[data-automation-id='pagination-page'] button:has-text('{page_number}')"
            btn = pg.locator(num_sel).first
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click(timeout=2500)
                    try:
                        pg.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pg.wait_for_timeout(800)
                    return True
            except Exception:
                pass
            return False

        # Page 1: parse HTML for externalPath
        html = page.content()
        for pth in collect_external_paths_from_html(html):
            seen_urls.add(build_detail(board_url, pth))
        print(f"[Zillow UI] page 1: {len(seen_urls)} links (label total={total_jobs or '?'}) search_clicked={clicked_search}")

        # Walk pages
        for pidx in range(2, 80):
            if not click_next_or_number(page, pidx):
                break
            before = len(seen_urls)
            html = page.content()
            for pth in collect_external_paths_from_html(html):
                seen_urls.add(build_detail(board_url, pth))
            gained = len(seen_urls) - before

            # refresh label if unknown
            if total_jobs is None:
                try:
                    txt = page.evaluate("() => document.body.innerText || ''")
                except Exception:
                    txt = ""
                total_jobs = parse_total_from_text(txt)

            print(f"[Zillow UI] page {pidx}: +{gained} (total {len(seen_urls)}/{total_jobs or '?'})")
            if total_jobs and len(seen_urls) >= total_jobs:
                break

        # Normalize rows (title from slug as fallback)
        for href in sorted(seen_urls):
            m = re.search(r"/job/[^/]+/([^/?#]+)", href)
            title = ""
            if m:
                slug = m.group(1)
                title = clean(re.sub(r"[_-]+", " ", re.sub(r"\bR?\d{4,}\b", "", slug)))
            out_rows.append({
                "source": "workday",
                "company": "Zillow",
                "title": title or "(Zillow role)",
                "location": "",
                "url": href
            })

        Path("zillow.json").write_text(json.dumps(out_rows, indent=2, ensure_ascii=False))
        print(f"[Zillow UI] final total: {len(out_rows)} jobs (label {total_jobs or '?'})")
        return out_rows

    finally:
        try: page.close()
        except Exception:
            pass

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

