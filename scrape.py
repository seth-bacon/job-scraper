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
    Zillow (Workday) — FULL pagination via CxS first, then DOM fallback.
      1) Use same-origin CxS JSON API to fetch all postings (limit/offset loop)
      2) Build items from JSON (title, locationsText, externalPath) — no detail visits needed
      3) If CxS fails/empty, fall back to DOM anchors + paginate/scroll + (optional) detail visits
      4) Save zillow_cxs.json for debugging
    """
    import json, re
    from pathlib import Path

    def clean(s):
        return re.sub(r"\s+", " ", (s or "").strip())

    def build_detail(board_url_, url_or_path: str) -> str:
        if url_or_path.startswith("http"):
            return url_or_path
        return board_url_.rstrip("/") + "/job/" + url_or_path.lstrip("/")

    out = []
    page = playwright_context.new_page()

    try:
        # --- Open board
        page.goto(board_url, wait_until="domcontentloaded", timeout=45000)

        # --- Derive tenant + board for the CxS endpoint from location
        try:
            tenant, board = page.evaluate("""
                () => {
                  const parts = location.pathname.split('/').filter(Boolean);
                  // e.g. /en-US/Zillow_Group_External  -> board = last segment
                  const board = parts[parts.length - 1];
                  const tenant = location.host.split('.')[0]; // 'zillow'
                  return { tenant, board };
                }
            """)
        except Exception:
            tenant, board = None, None

        # --- 1) CxS full pagination (preferred)
        postings = []
        if tenant and board:
            try:
                postings = page.evaluate(
                    """
                    async ({tenant, board}) => {
                      const api = `/wday/cxs/${tenant}/${board}/jobs`;
                      const limit = 50;
                      let offset = 0, all = [];
                      for (let i = 0; i < 40; i++) {           // up to 2000 jobs safe-guard
                        const resp = await fetch(api, {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ appliedFacets: {}, limit, offset, searchText: '' })
                        });
                        if (!resp.ok) break;
                        const data = await resp.json();
                        const batch = (data && data.jobPostings) || [];
                        all = all.concat(batch);
                        if (batch.length < limit) break;      // last page
                        offset += limit;
                      }
                      return all;
                    }
                    """,
                    {"tenant": tenant, "board": board}
                )
            except Exception:
                postings = []

        # Save CxS for debug & use it if non-empty
        try:
            Path("zillow_cxs.json").write_text(json.dumps(postings or [], indent=2))
        except Exception:
            pass

        if postings:
            # 2) Build items directly from JSON (no detail visits required)
            for j in postings:
                title = clean(j.get("title") or j.get("titleFacet") or "")
                locs = clean(j.get("locationsText") or "")
                ep = j.get("externalPath") or ""
                if ep:
                    href = build_detail(board_url, ep)
                    out.append({
                        "source": "workday",
                        "company": "Zillow",
                        "title": title or "(Zillow role)",
                        "location": locs,
                        "url": href
                    })
            # done — write json & return
            save_json("zillow.json", out)
            print(f"[DEBUG] Zillow via CxS: {len(out)} jobs")
            return out

        # --- 3) Fallback: DOM scraping + basic paginate/scroll
        # Accept cookies if present
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2500)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        def collect_links_dom():
            sel_js = """
              (sels) => {
                const seen = new Set(), acc = [];
                for (const sel of sels) {
                  document.querySelectorAll(sel).forEach(a => {
                    const href = a.href || a.getAttribute('href') || '';
                    if (!href) return;
                    const abs = href.startsWith('http') ? href : new URL(href, location.href).href;
                    if (!/(login|apply|search)/i.test(abs) && /\\/job\\//i.test(abs) && !seen.has(abs)) {
                      seen.add(abs); acc.push(abs);
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
            try:
                hrefs = page.evaluate(sel_js, selectors)
            except Exception:
                hrefs = []
            return list(dict.fromkeys(hrefs))

        seen = set(collect_links_dom())

        def try_paginate_once():
            for sel in [
                "[data-automation-id='pagination-next']",
                "button[aria-label='Next Page']",
                "button[aria-label='Next']",
                "button:has-text('Next')",
                "a[aria-label='Next']",
                "[data-automation-id='showMore']",
                "button:has-text('Show more')",
                "button:has-text('More Jobs')"
            ]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_enabled():
                    try:
                        loc.click(timeout=2500)
                        page.wait_for_load_state("networkidle", timeout=10000)
                        return True
                    except Exception:
                        pass
            return False

        if seen:
            for _ in range(20):  # try many times; some boards have lots of pages
                if not try_paginate_once():
                    break
                for u in collect_links_dom():
                    seen.add(u)
        else:
            # force-render by scrolling
            for _ in range(20):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(300)
            for u in collect_links_dom():
                seen.add(u)

        # Build minimal items from links (no detail visit to keep it quick)
        # Optionally, you can visit each link to enrich title/location if required.
        for href in sorted(seen):
            # derive a readable title from slug as a fallback
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

        save_json("zillow.json", out)
        print(f"[DEBUG] Zillow via DOM fallback: {len(out)} jobs")
        return out

    finally:
        try: page.close()
        except Exception: pass

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
