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

def scrape_zillow_workday(playwright_context, board_url: str):
    """
    Robust Zillow (Workday) scraper:
      1) Accept cookie banner (OneTrust) if present
      2) Wait for client render (networkidle) and try multiple link selectors
      3) Paginate via "Next" or "Show more"
      4) Force-render by scrolling
      5) Same-origin CxS JSON fallback; save zillow_cxs.json for debug
      6) Visit details for real title/location
      7) If nothing found, save zillow_debug.html to help inspect structure
    """
    import json, re
    from pathlib import Path

    page = playwright_context.new_page()
    out, seen = [], set()

    def clean(s): 
        return re.sub(r"\s+", " ", (s or "").strip())

    def collect_links():
        # Try a few common Workday link selectors and generic /job/ anchors
        sel_js = """
          (sels) => {
            const seen = new Set();
            const acc = [];
            for (const sel of sels) {
              document.querySelectorAll(sel).forEach(a => {
                const href = a.href || a.getAttribute('href') || '';
                if (!href) return;
                const abs = href.startsWith('http') ? href : new URL(href, location.href).href;
                if (!/(login|apply|search)/i.test(abs) && /\/job\//i.test(abs) && !seen.has(abs)) {
                  seen.add(abs);
                  acc.push(abs);
                }
              });
            }
            return acc;
          }
        """
        selectors = [
          "a[data-automation-id='jobTitle']",
          "a[data-automation-id='jobPostingTitle']",
          "a[href*='/job/']",
          "a[href*='/jobs/']"
        ]
        hrefs = page.evaluate(sel_js, selectors)
        return list(dict.fromkeys(hrefs))

    try:
        # 1) Open board & try to accept cookie banner
        page.goto(board_url, wait_until="networkidle", timeout=45000)

        # OneTrust accept button
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=3000)
        except Exception:
            pass

        # 2) First attempt to collect links
        links = collect_links()

        # 3) Pagination attempts: "Next" / "Show more"
        # Try up to ~8 page advances
        def try_paginate_once():
            # Next variants
            for sel in [
                "[data-automation-id='pagination-next']",
                "button[aria-label='Next Page']",
                "button[aria-label='Next']",
                "button:has-text('Next')",
                "a[aria-label='Next']"
            ]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_enabled():
                    try:
                        loc.click(timeout=2500)
                        page.wait_for_load_state("networkidle", timeout=10000)
                        return True
                    except Exception:
                        pass
            # Show more variants
            for sel in [
                "[data-automation-id='showMore']",
                "button:has-text('Show more')",
                "button:has-text('More Jobs')"
            ]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_enabled():
                    try:
                        loc.click(timeout=2500)
                        page.wait_for_timeout(1200)
                        return True
                    except Exception:
                        pass
            return False

        if links:
            for _ in range(8):
                for u in links:
                    seen.add(u)
                if not try_paginate_once():
                    break
                links = collect_links()

        # 4) Force render by scrolling if still empty
        if not seen:
            for _ in range(12):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(350)
            links = collect_links()
            for u in links:
                seen.add(u)

        # 5) Same-origin CxS fallback (and save JSON for debug)
        if not seen:
            try:
                postings = page.evaluate(
                    """
                    async () => {
                      const parts = location.pathname.split('/').filter(Boolean);
                      // workday locales look like /en-US/<boardSlug>
                      const board = parts[parts.length - 1];
                      const tenant = location.host.split('.')[0];
                      const api = `/wday/cxs/${tenant}/${board}/jobs`;
                      let out = [], offset = 0, limit = 50;
                      for (let i = 0; i < 8; i++) {
                        const resp = await fetch(api, {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ appliedFacets: {}, limit, offset, searchText: '' })
                        });
                        if (!resp.ok) break;
                        const data = await resp.json();
                        const jobs = (data && data.jobPostings) || [];
                        out = out.concat(jobs);
                        if (jobs.length < limit) break;
                        offset += limit;
                      }
                      return out;
                    }
                    """
                )
                # Save debug json
                try:
                    Path("zillow_cxs.json").write_text(json.dumps(postings, indent=2))
                except Exception:
                    pass
                print(f"[DEBUG] Zillow postings found: {len(postings) if postings else 0}")
                for j in postings or []:
                    ep = j.get("externalPath") or ""
                    if ep:
                        seen.add(board_url.rstrip("/") + "/job/" + ep)
            except Exception:
                pass

        # 6) Visit detail pages for real title/location
        final_links = list(seen)[:150]
        for href in final_links:
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                title = ""
                try:
                    title = page.locator("h1").first.text_content(timeout=6000) or ""
                except Exception:
                    pass
                title = clean(title)
                location = ""
                for sel in [".locations", "[data-automation-id='jobLocation']", "span.job-location"]:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            txt = el.text_content(timeout=2500) or ""
                            location = clean(txt)
                            if location:
                                break
                    except Exception:
                        pass
                if title:
                    out.append({
                        "source": "workday",
                        "company": "Zillow",
                        "title": title,
                        "location": location,
                        "url": href
                    })
            except Exception:
                pass

        # 7) If still nothing, dump board HTML for inspection
        if not out:
            try:
                Path("zillow_debug.html").write_text(page.content())
            except Exception:
                pass

    finally:
        try: page.close()
        except Exception: pass

    save_json("zillow.json", out)
    return out

# ----------------- main -----------------
def main():
    # env overrides (optional)
    apple_team_env = os.getenv("APPLE_TEAM_URLS", "")
    apple_team_urls = [u.strip() for u in apple_team_env.split(",") if u.strip()] or [
        "https://jobs.apple.com/en-us/search?team=software-quality-automation-and-tools-SFTWR-SQAT",
        "https://jobs.apple.com/en-us/search?team=quality-engineering-OPMFG-QE",
    ]
    zillow_board_url = os.getenv("ZILLOW_BOARD_URL", "https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External")

    print("Scraping Airbnb (Greenhouse)…")
    scrape_airbnb_greenhouse()

    print("Scraping Liberty Mutual (iCIMS)…")
    scrape_liberty_icims()

    print("Scraping Apple + Zillow (Playwright)…")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(user_agent=UA, locale="en-US")

        scrape_apple(context, apple_team_urls)
        scrape_zillow_workday(context, zillow_board_url)

        context.close()
        browser.close()

    print("Done. Wrote airbnb.json, libertymutual.json, apple.json, zillow.json")

if __name__ == "__main__":
    main()
