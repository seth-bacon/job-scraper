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

# ---- Zillow (Workday) via CxS API with requests (no Playwright) ----
def scrape_zillow_workday_cxs(board_url: str):
    """
    Hit Workday's CxS API directly to fetch ALL Zillow postings.
    Examples:
      board_url: https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External
      cxs url:   https://zillow.wd5.myworkdayjobs.com/wday/cxs/zillow/Zillow_Group_External/jobs
    """
    import json, re
    from urllib.parse import urlparse
    out = []

    def clean(s):
        return re.sub(r"\s+", " ", (s or "").strip())

    # Parse the URL to get host + last path segment (board)
    u = urlparse(board_url)
    host = u.netloc                          # e.g., zillow.wd5.myworkdayjobs.com
    parts = [p for p in u.path.split("/") if p]
    board = parts[-1] if parts else "Zillow_Group_External"

    # Tenant is the subdomain before the first dot (e.g., 'zillow')
    tenant = host.split(".")[0]

    cxs = f"https://{host}/wday/cxs/{tenant}/{board}/jobs"

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": f"https://{host}",
        "Referer": board_url,
    }

    limit = 50
    offset = 0
    max_pages = 60  # safety
    total = None

    for _ in range(max_pages):
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": ""
        }
        r = get(cxs, headers=headers) if False else requests.post(cxs, headers=headers, json=payload, timeout=40)
        if r.status_code != 200:
            print(f"[Zillow CxS] HTTP {r.status_code} at offset {offset}")
            break
        data = r.json()
        batch = (data or {}).get("jobPostings", [])
        if total is None:
            total = (data or {}).get("total", None)
        for j in batch:
            title = clean(j.get("title") or j.get("titleFacet") or "")
            locs  = clean(j.get("locationsText") or "")
            ep    = j.get("externalPath") or ""
            if ep:
                url = board_url.rstrip("/") + "/job/" + ep.lstrip("/")
                out.append({
                    "source": "workday",
                    "company": "Zillow",
                    "title": title or "(Zillow role)",
                    "location": locs,
                    "url": url
                })
        print(f"[Zillow CxS] got {len(batch)} (offset {offset}) total_so_far={len(out)} total_label={total}")
        if len(batch) < limit:
            break
        offset += limit

    save_json("zillow.json", out)
    print(f"[Zillow CxS] final: {len(out)} jobs (Workday total label: {total})")
    return out

# ----------------- main -----------------
def main():
    # ... (Airbnb + Liberty the same)

    print("Scraping Apple + Zillow…")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(user_agent=UA, locale="en-US")

        # Apple still needs Playwright
        scrape_apple(context, [
            "https://jobs.apple.com/en-us/search?team=software-quality-automation-and-tools-SFTWR-SQAT",
            "https://jobs.apple.com/en-us/search?team=quality-engineering-OPMFG-QE",
        ])

        context.close()
        browser.close()

    # 👉 Zillow via requests (no Playwright)
    scrape_zillow_workday_cxs("https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External")

    print("Done. Wrote airbnb.json, libertymutual.json, apple.json, zillow.json")

if __name__ == "__main__":
    main()
