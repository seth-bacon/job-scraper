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
    Zillow (Workday) — robust numbered pagination without fragile locators:
      - Clicks Search if required
      - On each page, parses HTML for "externalPath" and builds detail URLs
      - Reads "of N jobs" via document text (regex), not DOM locators
      - Clicks Next / numeric buttons until done
      - Writes zillow.json
    """
    import re, json
    from pathlib import Path

    def clean(s):
        return re.sub(r"\s+", " ", (s or "").strip())

    def parse_total_jobs_from_text(txt: str):
        # e.g., "1 - 20 of 118 jobs"
        m = re.search(r"\bof\s+(\d+)\s+jobs?\b", txt, flags=re.I)
        return int(m.group(1)) if m else None

    def collect_links_from_html(html: str):
        # Workday embeds job identifiers as "externalPath":"<path>"
        paths = re.findall(r'"externalPath"\s*:\s*"([^"]+)"', html)
        links = []
        seen = set()
        for pth in paths:
            # normalize
            href = board_url.rstrip("/") + "/job/" + pth.lstrip("/")
            if href not in seen:
                seen.add(href)
                links.append(href)
        return links

    def click_search_if_needed(page):
        for sel in [
            "[data-automation-id='searchButton']",
            "button:has-text('Search')",
        ]:
            btn = page.locator(sel).first
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click(timeout=2500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        page.wait_for_timeout(800)
                    return True
            except Exception:
                pass
        return False

    def click_next_or_number(page, page_number):
        # Try "Next" variants
        for sel in [
            "[data-automation-id='pagination-next']",
            "button[aria-label='Next Page']",
            "button[aria-label='Next']",
            "a[aria-label='Next']",
            "button:has-text('Next')",
            "//button[@data-uxi-element-id='next']",
        ]:
            btn = page.locator(sel).first
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click(timeout=2500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        page.wait_for_timeout(800)
                    return True
            except Exception:
                pass
        # Try numeric page button “2”, “3”, …
        num_sel = f"[data-automation-id='pagination-page'] button:has-text('{page_number}')"
        btn = page.locator(num_sel).first
        try:
            if btn.count() > 0 and btn.is_enabled():
                btn.click(timeout=2500)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    page.wait_for_timeout(800)
                return True
        except Exception:
            pass
        return False

    page = playwright_context.new_page()
    out, seen = [], set()
    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)

        # Accept OneTrust cookies if shown
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2000)
        except Exception:
            pass

        clicked_search = click_search_if_needed(page)

        # Read the total "of N jobs" from page text (no selectors)
        try:
            txt = page.evaluate("() => document.body.innerText || ''")
        except Exception:
            txt = ""
        total_jobs = parse_total_jobs_from_text(txt)

        # Page 1: collect via HTML externalPath
        html = page.content()
        links = collect_links_from_html(html)
        for u in links: seen.add(u)
        print(f"[Zillow] page 1: +{len(links)} (total {len(seen)}/{total_jobs or '?'}) search_clicked={clicked_search}")

        # Walk pages
        MAX_PAGES = 80
        for pidx in range(2, MAX_PAGES + 1):
            if not click_next_or_number(page, pidx):
                break
            before = len(seen)
            # refresh total in case label is only visible after first page
            try:
                txt = page.evaluate("() => document.body.innerText || ''")
            except Exception:
                txt = ""
            if total_jobs is None:
                total_jobs = parse_total_jobs_from_text(txt)

            html = page.content()
            links = collect_links_from_html(html)
            for u in links: seen.add(u)
            gained = len(seen) - before
            print(f"[Zillow] page {pidx}: +{gained} (total {len(seen)}/{total_jobs or '?'})")

            if total_jobs and len(seen) >= total_jobs:
                break

        # Normalize items (use slug as fallback title; Apps Script filters later)
        for href in sorted(seen):
            m = re.search(r"/job/[^/]+/([^/?#]+)", href)
            title = ""
            if m:
                slug = m.group(1)
                title = clean(re.sub(r"[_-]+", " ", re.sub(r"\bR?\d{4,}\b", "", slug)))
            out.append({
                "source": "workday",
                "company": "Zillow",
                "title": title or "(Zillow role)",
                "location": "",
                "url": href
            })

        Path("zillow.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"[Zillow] final total: {len(out)} jobs (board label says {total_jobs or '?'})")
        return out

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

