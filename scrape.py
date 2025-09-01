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
    Zillow (Workday) robust fetcher:
      Tier 1: requests -> CxS POST with full headers (limit/offset pagination)
      Tier 2: in-page CxS POST via Playwright (same-origin)
      Tier 3: UI pagination (Next / page numbers) via Playwright
    Writes zillow.json.
    """
    import json, re
    from urllib.parse import urlparse
    from pathlib import Path

    def clean(s): 
        return re.sub(r"\s+", " ", (s or "").strip())

    # ---------- common helpers ----------
    def cxs_url_from_board(u: str):
        p = urlparse(u)
        host = p.netloc                     # e.g. zillow.wd5.myworkdayjobs.com
        parts = [x for x in p.path.split("/") if x]
        board = parts[-1]                   # e.g. Zillow_Group_External
        tenant = host.split(".")[0]         # e.g. zillow
        return f"https://{host}/wday/cxs/{tenant}/{board}/jobs", host, board

    def build_detail(board_u: str, external_path: str):
        return board_u.rstrip("/") + "/job/" + external_path.lstrip("/")

    def cxs_payload(limit, offset):
        return {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": ""
        }

    def normalize_from_postings(postings):
        out = []
        for j in postings or []:
            ep = j.get("externalPath") or ""
            if not ep:
                continue
            out.append({
                "source": "workday",
                "company": "Zillow",
                "title": clean(j.get("title") or j.get("titleFacet") or "(Zillow role)"),
                "location": clean(j.get("locationsText") or ""),
                "url": build_detail(board_url, ep),
            })
        return out

    def save(rows):
        Path("zillow.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    # ---------- Tier 1: requests -> CxS ----------
    cxs, host, board = cxs_url_from_board(board_url)
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": f"https://{host}",
        "Referer": board_url,
        "Connection": "keep-alive",
    }

    limit, offset, all_posts = 50, 0, []
    try:
        for _ in range(60):  # safety
            r = requests.post(cxs, headers=headers, json=cxs_payload(limit, offset), timeout=40)
            if r.status_code != 200:
                print(f"[Zillow CxS Tier1] HTTP {r.status_code} at offset {offset}")
                all_posts = []
                break
            data = r.json() or {}
            batch = data.get("jobPostings", []) or []
            all_posts.extend(batch)
            print(f"[Zillow CxS Tier1] got {len(batch)} @offset {offset}, total {len(all_posts)}")
            if len(batch) < limit:
                break
            offset += limit

        if all_posts:
            rows = normalize_from_postings(all_posts)
            save(rows)
            print(f"[Zillow CxS Tier1] final {len(rows)} jobs")
            return rows
    except Exception as e:
        print(f"[Zillow CxS Tier1] error: {e}")

    # ---------- Tier 2: in-page CxS via Playwright (same-origin) ----------
    from playwright.sync_api import TimeoutError as PWTimeoutError
    page = playwright_context.new_page()
    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        # Accept OneTrust if present
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2500)
        except Exception:
            pass

        # Derive tenant/board on the page and POST via fetch()
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
            )
        except Exception as e:
            print(f"[Zillow CxS Tier2] eval error: {e}")
            postings = []

        if postings:
            rows = normalize_from_postings(postings)
            save(rows)
            print(f"[Zillow CxS Tier2] final {len(rows)} jobs")
            return rows
    finally:
        try:
            page.close()
        except Exception:
            pass

    # ---------- Tier 3: UI pagination (Next / numeric) ----------
    page = playwright_context.new_page()
    out, seen = [], set()
    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2500)
        except Exception:
            pass

        # Helper: collect links on current page
        def collect_links_here():
            js = """
              (sels) => {
                const acc = new Set();
                for (const sel of sels) {
                  document.querySelectorAll(sel).forEach(a => {
                    const href = a.href || a.getAttribute('href') || '';
                    if (!href) return;
                    const abs = href.startsWith('http') ? href : new URL(href, location.href).href;
                    if (!/(login|apply|search)/i.test(abs) && /\\/job\\//i.test(abs)) acc.add(abs);
                  });
                }
                return [...acc];
              }
            """
            sels = [
                "a[data-automation-id='jobTitle']",
                "a[data-automation-id='jobPostingTitle']",
                "a[href*='/job/']",
                "a[href*='/jobs/']",
            ]
            try:
                return page.evaluate(js, sels)
            except Exception:
                return []

        # Helper: click Next or page numbers
        def click_next_or_number(page_number):
            # Next variants
            for sel in [
                "[data-automation-id='pagination-next']",
                "button[aria-label='Next Page']",
                "button[aria-label='Next']",
                "a[aria-label='Next']",
                "button:has-text('Next')",
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
            # Numeric page button
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

        # Page 1
        for u in collect_links_here(): seen.add(u)
        print(f"[Zillow Tier3] page 1: {len(seen)} links")

        # Try to parse the "of N jobs" label for progress
        def parse_total():
            cand = [
                "[data-automation-id='pagination-label']",
                "[data-automation-id='pagination-info']"
            ]
            for sel in cand:
                el = page.locator(sel).first
                if el.count() > 0:
                    txt = el.text_content(timeout=800) or ""
                    m = re.search(r"\bof\s+(\d+)\s+jobs?\b", txt, flags=re.I)
                    if m: return int(m.group(1))
            return None
        total_label = parse_total()

        # Walk pages
        MAX_PAGES = 50
        for pidx in range(2, MAX_PAGES+1):
            if not click_next_or_number(pidx):
                break
            before = len(seen)
            for u in collect_links_here(): seen.add(u)
            gained = len(seen) - before
            t = total_label or "?"
            print(f"[Zillow Tier3] page {pidx}: +{gained} (total {len(seen)}/{t})")
            if total_label and len(seen) >= total_label:
                break

        # Normalize minimal items (slug as title fallback)
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

        save(out)
        print(f"[Zillow Tier3] final total: {len(out)} jobs")
        return out

    finally:
        try: page.close()
        except Exception: pass


# ----------------- main -----------------
def main():
    # ... (Airbnb + Liberty the same)

    print("Scraping Apple + Zillow…")
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    context = browser.new_context(user_agent=UA, locale="en-US")
    # Apple via Playwright (unchanged)
    scrape_apple(context, [
        "https://jobs.apple.com/en-us/search?team=software-quality-automation-and-tools-SFTWR-SQAT",
        "https://jobs.apple.com/en-us/search?team=quality-engineering-OPMFG-QE",
    ])
    # Zillow robust
    scrape_zillow_workday("https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External", context)
    context.close(); browser.close()

    # 👉 Zillow via requests (no Playwright)
    scrape_zillow_workday_cxs("https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External")

    print("Done. Wrote airbnb.json, libertymutual.json, apple.json, zillow.json")

if __name__ == "__main__":
    main()
