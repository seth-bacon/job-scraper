import json, os, time, re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ---------- Helpers ----------
def save_json(name, rows):
    Path(name).write_text(json.dumps(rows, indent=2, ensure_ascii=False))

def clean_text(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def get(url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    return requests.get(url, headers=headers, timeout=30, **kw)

# ---------- Airbnb (Greenhouse API) ----------
def scrape_airbnb_greenhouse():
    # Boards API: https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true
    url = "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true"
    r = get(url)
    r.raise_for_status()
    data = r.json()
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc = ""
        if j.get("location"):
            loc = j["location"].get("name", "") or ""
        job_url = j.get("absolute_url") or j.get("url") or ""
        out.append({
            "source": "greenhouse",
            "company": "Airbnb",
            "title": clean_text(title),
            "location": clean_text(loc),
            "url": job_url
        })
    save_json("airbnb.json", out)

# ---------- Liberty Mutual (iCIMS) ----------
def scrape_liberty_icims():
    base = "https://careers-libertymutual.icims.com"
    out = []
    seen = set()

    # A) Try sitemap first (fast)
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

    # B) Fallback: paginated search pages (?ss=1&pr=N)
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

# ---------- Apple & Zillow with Playwright (headless Chromium) ----------
def scrape_with_playwright():
    """
    Apple (Jobs): uses team URLs, list -> detail pages
    Zillow (Workday): loads board page, captures job detail links -> detail pages
    """
    apple_team_urls = [u.strip() for u in (os.getenv("APPLE_TEAM_URLS") or "").split(",") if u.strip()]
    if not apple_team_urls:
        apple_team_urls = [
            "https://jobs.apple.com/en-us/search?team=software-quality-automation-and-tools-SFTWR-SQAT",
            "https://jobs.apple.com/en-us/search?team=quality-engineering-OPMFG-QE",
        ]
    zillow_board_url = os.getenv("ZILLOW_BOARD_URL") or "https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External"

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(user_agent=UA, locale="en-US")

        # ---- Apple ----
        apple_out = []
        for team_url in apple_team_urls:
            page = context.new_page()
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
                links = links[:80]  # cap to keep it snappy
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
                        apple_out.append({
                            "source": "apple",
                            "company": "Apple",
                            "title": title or "(Apple role)",
                            "location": location,
                            "url": href
                        })
                    except Exception:
                        pass
            finally:
                page.close()
        save_json("apple.json", apple_out)

        # ---- Zillow (Workday) ----
        z_out = []
        page = context.new_page()
        try:
            page.goto(zillow_board_url, wait_until="domcontentloaded", timeout=30000)
            # Give client scripts a moment to render job links
            page.wait_for_timeout(3000)
            # pick up any visible job detail anchors
            anchors = list(set(page.eval_on_selector_all(
                "a[href*='/job/'], a[href*='/jobs/']",
                "els => els.map(a => a.href)"
            )))
            # filter out obvious non-detail links
            anchors = [a for a in anchors if not re.search(r"/(search|login|apply)", a, re.I)]
            anchors = anchors[:100]
            for href in anchors:
                try:
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    title = ""
                    try:
                        title = page.locator("h1").first.text_content(timeout=5000) or ""
                    except Exception:
                        pass
                    title = clean_text(title)
                    location = ""
                    # a few Workday location selectors
                    for sel in [".locations", "[data-automation-id='jobLocation']"]:
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0:
                                txt = el.text_content(timeout=2000) or ""
                                location = clean_text(txt)
                                if location: break
                        except Exception:
                            pass
                    if title:
                        z_out.append({
                            "source": "workday",
                            "company": "Zillow",
                            "title": title,
                            "location": location,
                            "url": href
                        })
                except Exception:
                    pass
        finally:
            page.close()
            context.close()
            browser.close()

        save_json("zillow.json", z_out)

def main():
    print("Scraping Airbnb (Greenhouse)…")
    scrape_airbnb_greenhouse()
    print("Scraping Liberty Mutual (iCIMS)…")
    scrape_liberty_icims()
    print("Scraping Apple + Zillow (Playwright)…")
    scrape_with_playwright()
    print("Done. Wrote airbnb.json, libertymutual.json, apple.json, zillow.json")

if __name__ == "__main__":
    main()
