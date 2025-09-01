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
    Zillow (Workday) — sniff API URL, then fetch all offsets directly.
    """
    import json, re
    from pathlib import Path
    from playwright.sync_api import Response

    def clean(s):
        return re.sub(r"\s+", " ", (s or "").strip())

    def build_detail(external_path: str) -> str:
        return board_url.rstrip("/") + "/job/" + external_path.lstrip("/")

    rows, seen_paths, seen_urls = [], set(), set()
    captured_posts = []
    api_url_seen = {"url": None}  # store API URL we sniff

    def normalize_and_add_from_posts(posts):
        added = 0
        for j in posts or []:
            ep = (j.get("externalPath") or "").strip()
            if not ep or ep in seen_paths:
                continue
            seen_paths.add(ep)
            href = build_detail(ep)
            if href in seen_urls:
                continue
            seen_urls.add(href)
            rows.append({
                "source": "workday",
                "company": "Zillow",
                "title": clean(j.get("title") or j.get("titleFacet") or "(Zillow role)"),
                "location": clean(j.get("locationsText") or ""),
                "url": href,
            })
            added += 1
        return added

    # --- Playwright page setup ---
    page = playwright_context.new_page()

    def on_response(resp: Response):
        try:
            url = resp.url
            if ("myworkdayjobs.com" in url or "workdayjobs.com" in url) and "/wday/" in url and "/jobs" in url:
                ctype = resp.headers.get("content-type", "")
                if "application/json" in ctype:
                    data = resp.json()
                    batch = (data.get("jobPostings") or data.get("items") or [])
                    if isinstance(batch, list) and batch:
                        captured_posts.extend(batch)
                        api_url_seen["url"] = url  # remember this API URL
                        print(f"[Zillow sniff] captured {len(batch)} from {url}")
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)

        # Accept cookie banner
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2000)
        except Exception:
            pass

        # Optional search click
        def click_search(pg):
            for sel in ("[data-automation-id='searchButton']",
                        "button[aria-label='Search']",
                        "button:has-text('Search')"):
                btn = pg.locator(sel).first
                if btn.count() > 0 and btn.is_enabled():
                    try:
                        btn.click(timeout=2000)
                        try:
                            pg.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pg.wait_for_timeout(800)
                        return True
                    except Exception:
                        pass
            return False

        clicked_search = click_search(page)

        # Let initial XHRs finish
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            page.wait_for_timeout(1000)

        if captured_posts:
            normalize_and_add_from_posts(captured_posts)
            captured_posts.clear()

        print(f"[Zillow sniff] after page 1: rows={len(rows)} search_clicked={clicked_search}")

        # ---- Direct fetch using sniffed API URL ----
        if api_url_seen["url"]:
            try:
                more = page.evaluate(
                    """
                    async (apiUrl) => {
                      const out = [];
                      const step = 20;
                      for (let offset = step; offset < 4000; offset += step) {
                        const resp = await fetch(apiUrl, {
                          method: 'POST',
                          headers: {'Content-Type':'application/json;charset=UTF-8'},
                          body: JSON.stringify({appliedFacets:{}, limit: step, offset, searchText: ''}),
                          credentials: 'same-origin'
                        });
                        if (!resp.ok) break;
                        const data = await resp.json();
                        const batch = (data && (data.jobPostings || data.items || [])) || [];
                        out.push(...batch);
                        if (batch.length < step) break;
                      }
                      return out;
                    }
                    """,
                    api_url_seen["url"]
                ) or []
                added = normalize_and_add_from_posts(more)
                print(f"[Zillow direct] fetched {len(more)} jobs via {api_url_seen['url']} (added {added})")

                if added > 0:
                    Path("zillow.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
                    print(f"[Zillow] final total: {len(rows)} jobs (bypassed UI pagination)")
                    return rows
            except Exception as e:
                print(f"[Zillow direct] error: {e}")

        # ---- Fallback: nothing fetched ----
        Path("zillow.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        print(f"[Zillow] final total: {len(rows)} jobs (fallback)")
        return rows

    finally:
        try:
            page.close()
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

