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
    Zillow (Workday) — frame autodetect + HTML parsing:
      - Enumerate all frames; choose the one that has the most "externalPath" occurrences
      - Click Search inside that frame if present
      - Parse "externalPath" from that frame's HTML on each page (shadow-DOM safe)
      - Click Next / numeric pagination inside that frame
      - Write debug artifacts: zillow_frames.json and zillow_frame_*.html
    """
    import re, json
    from pathlib import Path

    def clean(s):
        import re as _re
        return _re.sub(r"\s+", " ", (s or "").strip())

    def parse_total_from_text(txt: str):
        m = re.search(r"\bof\s+(\d+)\s+jobs?\b", txt, flags=re.I)
        return int(m.group(1)) if m else None

    def collect_links_from_html(html: str):
        # Extract detail paths even if anchors are inside shadow DOM
        paths = re.findall(r'"externalPath"\s*:\s*"([^"]+)"', html)
        return paths

    def click_search_if_needed(ctx):
        for sel in (
            "[data-automation-id='searchButton']",
            "button[aria-label='Search']",
            "button:has-text('Search')",
        ):
            btn = ctx.locator(sel).first
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click(timeout=2500)
                    try:
                        ctx.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        ctx.wait_for_timeout(800)
                    return True
            except Exception:
                pass
        return False

    def click_next_or_number(ctx, page_number):
        # Next variants inside the frame
        for sel in (
            "[data-automation-id='pagination-next']",
            "button[aria-label='Next Page']",
            "button[aria-label='Next']",
            "a[aria-label='Next']",
            "button:has-text('Next')",
            "//button[@data-uxi-element-id='next']",
        ):
            btn = ctx.locator(sel).first
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click(timeout=2500)
                    try:
                        ctx.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        ctx.wait_for_timeout(800)
                    return True
            except Exception:
                pass
        # Numeric page button inside the frame
        num_sel = f"[data-automation-id='pagination-page'] button:has-text('{page_number}')"
        btn = ctx.locator(num_sel).first
        try:
            if btn.count() > 0 and btn.is_enabled():
                btn.click(timeout=2500)
                try:
                    ctx.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    ctx.wait_for_timeout(800)
                return True
        except Exception:
            pass
        return False

    # -------------- run --------------
    page = playwright_context.new_page()
    out_rows, seen_urls = [], set()

    try:
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)

        # Accept cookies on the OUTER page, if shown
        try:
            if page.locator("#onetrust-accept-btn-handler").first.count() > 0:
                page.click("#onetrust-accept-btn-handler", timeout=2000)
        except Exception:
            pass

        # DEBUG: dump every frame (url + first 200 KB of HTML) and count "externalPath"
        frames_report = []
        for i, fr in enumerate(page.frames):
            fr_url = fr.url or ""
            try:
                html = fr.content()
            except Exception:
                html = ""
            count = html.count('"externalPath"')
            frames_report.append({"index": i, "url": fr_url, "externalPath_count": count, "html_len": len(html)})
            try:
                # Keep dumps small to avoid huge commits
                Path(f"zillow_frame_{i}.html").write_text(html[:200000])
            except Exception:
                pass
            print(f"[Zillow][frames] #{i} count={count} url={fr_url}")

        # Save report
        try:
            Path("zillow_frames.json").write_text(json.dumps(frames_report, indent=2))
        except Exception:
            pass

        # Pick the frame with the highest externalPath count; if all zero, prefer workday host
        target = None
        if frames_report:
            best = max(frames_report, key=lambda r: r["externalPath_count"])
            if best["externalPath_count"] > 0:
                target = page.frames[best["index"]]
            else:
                # fall back: first frame whose URL looks like a Workday results host
                for r in frames_report:
                    if "myworkdayjobs" in (r["url"] or "") or "workdayjobs" in (r["url"] or ""):
                        target = page.frames[r["index"]]
                        break

        if target is None:
            print("[Zillow] ERROR: no suitable Workday frame found; see zillow_frame_*.html and zillow_frames.json")
            Path("zillow.json").write_text("[]")
            return []

        # Some tenants require an explicit Search click inside the chosen frame
        clicked_search = click_search_if_needed(target)

        # Recompute totals from inner frame text (optional)
        try:
            inner_text = target.evaluate("() => document.body.innerText || ''")
        except Exception:
            inner_text = ""
        total_jobs = parse_total_from_text(inner_text)

        # Helper to add links from the current frame
        def add_current_page():
            html = target.content()
            paths = collect_links_from_html(html)
            added = 0
            for p in paths:
                href = board_url.rstrip("/") + "/job/" + p.lstrip("/")
                if href not in seen_urls:
                    seen_urls.add(href)
                    added += 1
            return added

        # Page 1
        gained = add_current_page()
        print(f"[Zillow] page 1: +{gained} (total {len(seen_urls)}/{total_jobs or '?'}) search_clicked={clicked_search}")

        # Walk pages
        for pidx in range(2, 80):
            if not click_next_or_number(target, pidx):
                break
            before = len(seen_urls)
            gained = add_current_page()

            # Refresh total label if still unknown
            if total_jobs is None:
                try:
                    inner_text = target.evaluate("() => document.body.innerText || ''")
                except Exception:
                    inner_text = ""
                total_jobs = parse_total_from_text(inner_text)

            print(f"[Zillow] page {pidx}: +{gained} (total {len(seen_urls)}/{total_jobs or '?'})")
            if total_jobs and len(seen_urls) >= total_jobs:
                break

        # Normalize items (title from slug as a fallback)
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
        print(f"[Zillow] final total: {len(out_rows)} jobs (board label says {total_jobs or '?'})")
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

