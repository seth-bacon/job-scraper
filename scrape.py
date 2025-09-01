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
    Zillow (Workday) — numbered pagination walker
      - Handles iframe or top-level page
      - Collects detail links per page
      - Clicks Next / numeric pagination until exhausted
      - De-dups links and writes zillow.json
    """
    import re

    def clean(s):
        return re.sub(r"\s+", " ", (s or "").strip())

    def get_workday_frame(page):
        # Some tenants embed in an iframe; pick frame that contains job anchors
        for fr in page.frames:
            try:
                # Workday content host often includes myworkdayjobs / workdayjobs
                if fr.url and ("myworkdayjobs" in fr.url or "workdayjobs" in fr.url):
                    # sanity: does this frame have job anchors?
                    if fr.locator("a[data-automation-id='jobTitle']").first.count() > 0 or \
                       fr.locator("a[href*='/job/']").first.count() > 0:
                        return fr
            except Exception:
                pass
        return page  # fallback to main page

    def collect_links(ctx):
        js = """
          (sels) => {
            const acc = new Set();
            for (const sel of sels) {
              document.querySelectorAll(sel).forEach(a => {
                const href = a.href || a.getAttribute('href') || '';
                if (!href) return;
                const abs = href.startsWith('http') ? href : new URL(href, location.href).href;
                if (!/(login|apply|search)/i.test(abs) && /\\/job\\//i.test(abs)) {
                  acc.add(abs);
                }
              });
            }
            return [...acc];
          }
        """
        selectors = [
          "a[data-automation-id='jobTitle']",
          "a[data-automation-id='jobPostingTitle']",
          "a[href*='/job/']",
          "a[href*='/jobs/']",
        ]
        try:
            return ctx.evaluate(js, selectors)
        except Exception:
            return []

    def visible_enabled(loc):
        try:
            return loc.count() > 0 and loc.is_visible(timeout=400) and loc.is_enabled()
        except Exception:
            return False

    def parse_total_jobs(ctx):
        # Try to read "1 - 20 of 118 jobs" label
        candidates = [
            "[data-automation-id='pagination-label']",
            "[data-automation-id='pagination-info']",
            "div:has-text('of') >> text=/\\bof\\b/",  # fallback text search
        ]
        for sel in candidates:
            try:
                el = ctx.locator(sel).first
                if el.count() > 0:
                    txt = el.text_content(timeout=1000) or ""
                    m = re.search(r"\bof\s+(\d+)\s+jobs?\b", txt, flags=re.I)
                    if m:
                        return int(m.group(1)), txt.strip()
            except Exception:
                pass
        return None, ""

    page = playwright_context.new_page()
    out, seen = [], set()

    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)

        # Cookie banner
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2000)
        except Exception:
            pass

        ctx = get_workday_frame(page)

        # Initial collect
        links = collect_links(ctx)
        for u in links: seen.add(u)

        total_jobs, label = parse_total_jobs(ctx)
        print(f"[Zillow] page 1: +{len(links)} (total {len(seen)}) label='{label}'")

        # Pagination loop — prefer "Next Page" control; if absent, try numeric pages
        MAX_PAGES = 50
        for page_idx in range(2, MAX_PAGES + 1):
            clicked = False

            # Try Next-page variants
            for sel in [
                "[data-automation-id='pagination-next']",
                "button[aria-label='Next Page']",
                "button[aria-label='Next']",
                "a[aria-label='Next']",
                "button:has-text('Next')",
            ]:
                btn = ctx.locator(sel).first
                if visible_enabled(btn):
                    try:
                        btn.click(timeout=2500)
                        # wait for re-render
                        try:
                            ctx.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            ctx.wait_for_timeout(800)
                        clicked = True
                        break
                    except Exception:
                        pass
            # If no "Next", try direct numeric page button “2”, “3”, …
            if not clicked:
                num_sel = f"[data-automation-id='pagination-page'] button:has-text('{page_idx}')"
                btn = ctx.locator(num_sel).first
                if visible_enabled(btn):
                    try:
                        btn.click(timeout=2500)
                        try:
                            ctx.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            ctx.wait_for_timeout(800)
                        clicked = True
                    except Exception:
                        pass

            if not clicked:
                # No more pages
                break

            # Collect after navigation
            before = len(seen)
            for u in collect_links(ctx): seen.add(u)
            gained = len(seen) - before

            total_jobs, label = parse_total_jobs(ctx)
            if total_jobs:
                print(f"[Zillow] page {page_idx}: +{gained} (total {len(seen)}/{total_jobs}) label='{label}'")
                # Stop early if we’ve gathered all items the label says exist
                if len(seen) >= total_jobs:
                    break
            else:
                print(f"[Zillow] page {page_idx}: +{gained} (total {len(seen)})")

            # If a page added nothing, try one extra advance; otherwise bail
            if gained == 0:
                # try one more "Next" click; if still nothing, stop
                tried_extra = False
                for sel in [
                    "[data-automation-id='pagination-next']",
                    "button[aria-label='Next Page']",
                    "button[aria-label='Next']",
                    "a[aria-label='Next']",
                    "button:has-text('Next')",
                ]:
                    btn = ctx.locator(sel).first
                    if visible_enabled(btn):
                        try:
                            btn.click(timeout=2500)
                            try:
                                ctx.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                ctx.wait_for_timeout(800)
                            tried_extra = True
                            break
                        except Exception:
                            pass
                if not tried_extra:
                    break
                # collect again
                before2 = len(seen)
                for u in collect_links(ctx): seen.add(u)
                if len(seen) == before2:
                    break

        # Build minimal items (use slug as title fallback; Apps Script filters on title/location later)
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

        save_json("zillow.json", out)
        tj = total_jobs if total_jobs is not None else "?"
        print(f"[Zillow] final total: {len(out)} jobs (board label says {tj})")
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
