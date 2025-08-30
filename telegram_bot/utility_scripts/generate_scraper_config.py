from __future__ import annotations

import argparse
from pathlib import Path
import json
from textwrap import dedent
import re
import urllib.parse as urlparse
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup, Tag


DEFAULT_MATCHING_BLOCK = """
matching:
  fuzz_scorer: token_set_ratio
  fuzz_threshold: 88
""".strip()


TEMPLATE = """
site_name: "{site_name}"
base_url: "{base_url}"

# Map internal media types to site categories
category_mapping:
  movie: {movie_category}
  tv: {tv_category}

# Search path expects query, category, and page number
search_path: "{search_path}"

# CSS selectors for parsing the initial results page
results_page_selectors:
  # The table body contains all search results. Limiting the scope avoids
  # parsing unrelated parts of the page.
  results_container: "{results_container}"
  # Each <tr> inside the container represents a single torrent result.
  result_row: "{result_row}"
  # Granular selectors used by the scraper when extracting data from a row.
  name: "{name_selector}"
  # If this points to a detail page, the scraper will fetch it to resolve a magnet.
  magnet: "{magnet_selector}"
  seeders: "{seeders_selector}"
  leechers: "{leechers_selector}"
  size: "{size_selector}"
  uploader: "{uploader_selector}"

# Selectors for elements on the torrent detail page
details_page_selectors:
  # Use a robust selector that targets any link beginning with 'magnet:'
  magnet_url: "{details_magnet_selector}"

# Matching configuration to fine-tune fuzzy filtering
{matching_block}
""".lstrip()


def _prompt(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def _prompt_choice(prompt: str, choices: list[str], default: str) -> str:
    choices_lc = [c.lower() for c in choices]
    default_lc = default.lower()
    assert default_lc in choices_lc
    disp = "/".join(choices)
    while True:
        val = _prompt(f"{prompt} ({disp})", default)
        v = val.strip().lower()
        if v in choices_lc:
            return v
        print(f"Please choose one of: {disp}")


def build_content(args: argparse.Namespace) -> str:
    """Build YAML content using auto-detected values or fallbacks.

    If only a site name is provided, the function attempts to:
    - Guess and validate the base URL
    - Discover a working search URL pattern (path template)
    - Infer selectors for results table and fields
    """

    site_name_input = args.name or _prompt("Site name (e.g., 1337x)")

    # 1) Resolve base URL (may be overridden later by example/search URL)
    base_url = (args.base_url or guess_base_url(site_name_input) or "").rstrip("/")
    if not base_url:
        base_url = _prompt("Base URL (auto-detect failed)", "https://example.com")
    # Prefer a canonical site name derived from the base URL if the provided name
    # looks like a URL or includes path separators.
    site_name = (
        derive_site_name_from_base(base_url)
        if site_name_input.startswith(("http://", "https://")) or "/" in site_name_input
        else site_name_input
    )

    # New flow: ask user for an example search URL and the search term used there.
    manual_search_url = getattr(args, "search_url", None)
    example_url = getattr(args, "example_url", None) or manual_search_url
    example_term = getattr(args, "example_query", None)
    if not example_url:
        example_url = _prompt(
            "Paste an example search URL from this site",
            "",
        )
    if not example_term:
        # Try to guess a default example term from the last path segment
        try:
            parsed_tmp = urlparse.urlparse(example_url)
            last_seg = (parsed_tmp.path.rstrip("/").split("/") or [""])[-1]
            default_term = last_seg.replace("-", " ").replace("_", " ")
        except Exception:
            default_term = ""
        example_term = _prompt(
            "What was the search term used in that example URL?", default_term
        )

    derived_base, derived_path = _derive_template_from_example(
        example_url, example_term
    )
    if derived_base:
        base_url = derived_base.rstrip("/")
    search_path_manual = derived_path

    # 2) Attempt to discover a working search path and selectors (unless provided)
    detection = None
    if search_path_manual is None and not any(
        [
            args.search_path,
            args.results_container,
            args.result_row,
            args.name_selector,
            args.magnet_selector,
            args.seeders_selector,
            args.leechers_selector,
            args.size_selector,
        ]
    ):
        try:
            detection = autodetect_from_site(base_url)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Auto-detection failed: {exc}")
            detection = None

    # 3) Fill with detected values or sensible defaults / prompts
    movie_category = (
        args.movie_category or (detection and detection.get("movie_cat")) or "Movies"
    )
    tv_category = args.tv_category or (detection and detection.get("tv_cat")) or "TV"

    # Prefer derived template from example; else any explicit --search-path; else autodetected; else fallback
    search_path = (
        search_path_manual
        or args.search_path
        or (detection and detection.get("search_path"))
        or "/category-search/{query}/{category}/{page}/"
    )

    results_container = (
        args.results_container
        or (detection and detection.get("results_container"))
        or "table tbody"
    )
    result_row = args.result_row or (detection and detection.get("result_row")) or "tr"
    name_selector = (
        args.name_selector or (detection and detection.get("name")) or "td a"
    )
    magnet_selector = (
        args.magnet_selector
        or (detection and detection.get("magnet"))
        or "a[href^='/torrent/'], a[href*='details'], a[href*='view']"
    )
    seeders_selector = (
        args.seeders_selector
        or (detection and detection.get("seeders"))
        or "td:nth-of-type(5)"
    )
    leechers_selector = (
        args.leechers_selector
        or (detection and detection.get("leechers"))
        or "td:nth-of-type(6)"
    )
    size_selector = (
        args.size_selector
        or (detection and detection.get("size"))
        or "td:nth-of-type(4)"
    )
    uploader_selector = (
        args.uploader_selector
        or (detection and detection.get("uploader"))
        or "td a[href*='user'], td:nth-of-type(8) a"
    )
    details_magnet_selector = (
        args.details_magnet_selector
        or (detection and detection.get("details_magnet"))
        or "a[href^='magnet:']"
    )

    matching_block = DEFAULT_MATCHING_BLOCK
    if args.matching is not None:
        matching_block = dedent(args.matching).strip() or DEFAULT_MATCHING_BLOCK

    return TEMPLATE.format(
        site_name=site_name,
        base_url=base_url.rstrip("/"),
        movie_category=movie_category,
        tv_category=tv_category,
        search_path=search_path,
        results_container=results_container,
        result_row=result_row,
        name_selector=name_selector,
        magnet_selector=magnet_selector,
        seeders_selector=seeders_selector,
        leechers_selector=leechers_selector,
        size_selector=size_selector,
        uploader_selector=uploader_selector,
        details_magnet_selector=details_magnet_selector,
        matching_block=matching_block,
    )


def default_output_path(site_name: str) -> Path:
    base = Path(__file__).resolve().parent.parent / "scrapers" / "configs"
    filename = f"{site_name.lower().replace(' ', '_')}.yaml"
    return base / filename


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a YAML scraper config for a torrent index site. "
            "It writes a commented template compatible with GenericTorrentScraper."
        )
    )

    # High-level fields
    parser.add_argument(
        "name", nargs="?", help="Site name or URL (e.g., 1337x or https://1337x.to)"
    )
    parser.add_argument(
        "--name", dest="name_flag", help="Alias for --name (backward compat)"
    )
    parser.add_argument("--base-url", help="Base URL, e.g. https://1337x.to")
    parser.add_argument("--movie-category", help="Category path for movies")
    parser.add_argument("--tv-category", help="Category path for TV")
    parser.add_argument(
        "--search-path",
        help="Search path with {query}/{category}/{page} placeholders",
    )

    # Selectors
    parser.add_argument("--results-container")
    parser.add_argument("--result-row")
    parser.add_argument("--name-selector")
    parser.add_argument("--magnet-selector")
    parser.add_argument("--seeders-selector")
    parser.add_argument("--leechers-selector")
    parser.add_argument("--size-selector")
    parser.add_argument("--uploader-selector")
    parser.add_argument("--details-magnet-selector")
    parser.add_argument(
        "--search-url",
        help=(
            "Full search URL or path template including {query}. If a full URL is"
            " provided, base_url is derived automatically. Example:"
            " '/search.php?q={query}&all=on&page=0'"
        ),
    )
    parser.add_argument(
        "--example-url", help="Example search results URL from the site"
    )
    parser.add_argument(
        "--example-query",
        help="The search term you used for the provided example URL",
    )

    parser.add_argument(
        "--matching",
        help=(
            "Override matching YAML block. Supply full YAML snippet. "
            "Default uses token_set_ratio with threshold 88."
        ),
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "Output path. Defaults to telegram_bot/scrapers/configs/<site>.yaml "
            "based on --name."
        ),
    )
    parser.add_argument(
        "--media",
        choices=["movies", "tv", "both"],
        help="Which results this site is used for in config.ini (movies, tv, or both).",
    )

    args = parser.parse_args()

    # Normalize name from positional or --name
    if not getattr(args, "name", None) and getattr(args, "name_flag", None):
        args.name = args.name_flag

    # We need a site name (or URL) to compute default output path if -o not provided
    raw_name = args.name or _prompt("Site name or URL (for file path)")
    # Ensure build_content sees a name so we don't prompt twice
    if not getattr(args, "name", None):
        args.name = raw_name
    if raw_name.startswith(("http://", "https://")):
        site_name_for_path = urlparse.urlparse(raw_name).netloc or raw_name
    else:
        site_name_for_path = raw_name
    out_path = args.output or default_output_path(site_name_for_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    content = build_content(args)
    out_path.write_text(content, encoding="utf-8")
    print(f"[OK] Wrote scraper config to: {out_path}")

    # Also update config.ini [search].websites with this site
    try:
        site_cfg = _yaml_safe_load_from_string(content)
        site_name = site_cfg.get("site_name") or derive_site_name_from_base(
            site_cfg.get("base_url", "")
        )
        base_url = str(site_cfg.get("base_url", "")).rstrip("/")
        search_path = str(site_cfg.get("search_path", ""))
        category_mapping = site_cfg.get("category_mapping", {}) or {}
        movie_cat = category_mapping.get("movie", "Movies")
        tv_cat = category_mapping.get("tv", "TV")
        # Determine media scope (movies/tv/both)
        scope = args.media or _prompt_choice(
            "Is this site for movies, tv, or both?", ["movies", "tv", "both"], "both"
        )
        media_scopes = [scope] if scope in ("movies", "tv") else ["movies", "tv"]
        _update_config_ini_with_site(
            site_name,
            base_url,
            search_path,
            movie_cat,
            tv_cat,
            media_scopes=media_scopes,
        )
        print("[OK] Updated config.ini [search].websites with new site entries")
    except Exception as exc:
        print(f"[WARN] Could not update config.ini automatically: {exc}")


# ---------------------- Auto-detection helpers ----------------------

# Known site name -> canonical base URL shortcuts
KNOWN_SITES: dict[str, str] = {
    "1337x": "https://1337x.to",
    "1337x.to": "https://1337x.to",
    "eztv": "https://eztv.re",
    "eztv.re": "https://eztv.re",
    "yts": "https://yts.mx",
    "yts.mx": "https://yts.mx",
    "torrentgalaxy": "https://torrentgalaxy.to",
    "torrentgalaxy.to": "https://torrentgalaxy.to",
}


def guess_base_url(site: str) -> Optional[str]:
    """Guess and validate a base URL from a name or URL.

    Tries KNOWN_SITES first, then probes common TLDs.
    """
    site = site.strip()
    if not site:
        return None

    # If it's already a URL, return its normalized base
    if site.startswith("http://") or site.startswith("https://"):
        try:
            parsed = urlparse.urlparse(site)
            url = f"{parsed.scheme}://{parsed.netloc}"
            return url if _probe_url(url) else None
        except Exception:
            return None

    key = site.lower()
    if key in KNOWN_SITES:
        url = KNOWN_SITES[key]
        return url if _probe_url(url) else url

    # Try common TLDs and prefixes
    candidates: list[str] = []
    tlds = ["to", "re", "se", "sx", "st", "ag", "ws", "is", "mx", "si"]
    for tld in tlds:
        candidates.append(f"https://{key}.{tld}")
        candidates.append(f"https://www.{key}.{tld}")

    for url in candidates:
        if _probe_url(url):
            return url

    return None


def _probe_url(url: str) -> bool:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0 Safari/537.36"
            )
        }
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            return r.status_code < 400 and bool(r.text)
    except Exception:
        return False


def autodetect_from_site(base_url: str) -> dict[str, Any]:
    """Fetch a search results page and infer selectors and path template.

    Heuristic approach:
    - Try common search URL templates with a seed query
    - Choose the first response that yields a plausible results table
    - Infer columns (name/seeders/leechers/size/uploader) via header matching
    - Derive CSS selectors that are resilient but simple
    """
    seed_query = "inception"
    # Priority 1: discover a search form on the homepage
    discovered_tpl = _discover_search_template(base_url)
    # Priority 2: common fallbacks
    candidates = ([discovered_tpl] if discovered_tpl else []) + [
        "/category-search/{query}/Movies/1/",
        "/search/{query}/1/",
        "/search/{query}/",
        "/search/{query}",
        "/search.php?q={query}",
        "/search?q={query}",
        "/torrents.php?search={query}",
        "/usearch/{query}/",
        "/s/{query}",
    ]

    html = None
    chosen_template = None
    for tpl in candidates:
        url = urlparse.urljoin(
            base_url, tpl.format(query=urlparse.quote_plus(seed_query))
        )
        html = _get(url)
        if not html:
            continue
        if _looks_like_results_page(html):
            chosen_template = tpl
            # Try to refine template with pagination parameter if present
            try:
                soup_tmp = BeautifulSoup(html, "lxml")
                chosen_template = _refine_with_pagination(
                    base_url,
                    tpl,
                    url,
                    soup_tmp,
                    seed_query,
                )
            except Exception:
                pass
            break

    if not html or not chosen_template:
        raise RuntimeError("No viable search page detected")

    soup = BeautifulSoup(html, "lxml")
    table, meta = _pick_results_table(soup)
    if not table:
        raise RuntimeError("Could not detect a results table")

    # Build selectors
    container_sel = _css_for_table(table, soup) + " tbody"
    row_sel = "tr"

    col_map = _infer_columns(table)
    # Name selector: prefer a title/detail anchor not magnet
    name_selector = col_map.get("name_selector", "td a")
    magnet_selector = col_map.get("magnet_selector", "a[href^='magnet:']")
    seeders_selector = col_map.get("seeders_selector", "td:nth-of-type(5)")
    leechers_selector = col_map.get("leechers_selector", "td:nth-of-type(6)")
    size_selector = col_map.get("size_selector", "td:nth-of-type(4)")
    uploader_selector = col_map.get("uploader_selector", "td a[href*='user']")

    # For 1337x-like, upgrade template to include category/page placeholders
    search_path = _upgrade_template_with_placeholders(chosen_template)

    return {
        "search_path": search_path,
        "results_container": container_sel,
        "result_row": row_sel,
        "name": name_selector,
        "magnet": magnet_selector,
        "seeders": seeders_selector,
        "leechers": leechers_selector,
        "size": size_selector,
        "uploader": uploader_selector,
        "details_magnet": "a[href^='magnet:']",
        # Heuristic default cats used by 1337x-like sites
        "movie_cat": "Movies",
        "tv_cat": "TV",
    }


def _get(url: str) -> Optional[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/115.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
    }
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            if r.status_code >= 400:
                return None
            return r.text
    except Exception:
        return None


def _looks_like_results_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    # Fast heuristics: presence of many rows, numbers for seeds/leech, magnet anchors
    if soup.select_one("a[href^='magnet:']"):
        return True
    headers = " ".join(th.get_text(" ", strip=True).lower() for th in soup.select("th"))
    if any(key in headers for key in ["seed", "leech", "size"]):
        return True
    # Many rows in a table
    for table in soup.select("table"):
        rows = table.select("tr")
        if len(rows) >= 10:
            return True
    return False


def _discover_search_template(base_url: str) -> Optional[str]:
    """Discover a search URL template by parsing a site's homepage search form.

    Heuristics:
    - Prefer forms with method GET and action containing 'search'.
    - Pick a query input whose name looks like one of: q, query, search, s, keywords.
    - Include hidden inputs with constant values in the template.
    - Return a path or query-string template containing {query}.
    """
    html = _get(base_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    forms = soup.find_all("form")
    if not forms:
        return None

    def score_form(form: Tag) -> int:
        score = 0
        method = (_get_attr_str(form, "method") or "get").lower()
        if method == "get":
            score += 2
        action = (_get_attr_str(form, "action") or "").lower()
        if "search" in action:
            score += 2
        # Inputs
        inputs = form.find_all("input")
        for inp in inputs:
            if not isinstance(inp, Tag):
                continue
            itype = (_get_attr_str(inp, "type") or "").lower()
            name = (_get_attr_str(inp, "name") or "").lower()
            if itype in ("search", "text"):
                score += 1
            if name in ("q", "query", "search", "s", "keywords", "qf", "term"):
                score += 2
        return score

    best: Optional[Tag] = None
    best_score = -1
    for form in forms:
        if not isinstance(form, Tag):
            continue
        sc = score_form(form)
        if sc > best_score:
            best_score = sc
            best = form

    if not isinstance(best, Tag):
        return None

    # Determine action
    action_url = _get_attr_str(best, "action") or "/"
    if not action_url.startswith("http"):
        action_url = urlparse.urljoin(base_url + "/", action_url)
    base, path_q = _split_base_and_path(action_url)
    # Determine query parameter name
    q_name = None
    hidden_params: list[tuple[str, str]] = []
    for inp in best.find_all("input"):
        if not isinstance(inp, Tag):
            continue
        name = _get_attr_str(inp, "name")
        if not name:
            continue
        itype = (_get_attr_str(inp, "type") or "text").lower()
        if itype in ("text", "search") and q_name is None:
            q_name = name
        elif itype == "hidden":
            val = _get_attr_str(inp, "value")
            if val:
                hidden_params.append((name, val))

    if q_name is None:
        # fallback: pick first text-like input name
        for inp in best.find_all("input"):
            if not isinstance(inp, Tag):
                continue
            name = _get_attr_str(inp, "name")
            if name:
                q_name = name
                break

    if not q_name:
        return None

    # Build template
    if "?" in path_q:
        # Merge into existing query string
        # Remove any existing params of q_name to avoid duplicates
        try:
            parsed = urlparse.urlparse(path_q)
            qs = urlparse.parse_qsl(parsed.query, keep_blank_values=True)
            qs = [(k, v) for (k, v) in qs if k != q_name]
            # Insert our q param first
            qs = [(q_name, "{query}")] + qs
            # Ensure constant hidden inputs are present if not already
            for k, v in hidden_params:
                if all(k2 != k for k2, _ in qs):
                    qs.append((k, v))
            new_query = urlparse.urlencode(qs, doseq=True)
            path_q = parsed.path + ("?" + new_query if new_query else "")
        except Exception:
            # Fallback: append our param
            sep = "&" if path_q.endswith("}") or "?" in path_q else "?"
            path_q = f"{path_q}{sep}{q_name}={{query}}"
    else:
        # Simple path -> add our query
        path_q = f"{path_q}?{q_name}={{query}}"

    return path_q


def _refine_with_pagination(
    base_url: str,
    template: str,
    current_url: str,
    soup: BeautifulSoup,
    seed_query: str,
) -> str:
    """Try to add a {page} placeholder by inspecting 'next' links on results.

    If a pagination parameter is detected in the next page's href and is absent
    from the template, it will be added. Supports both query-string (?page=1)
    and path-segment (/1/) styles.
    """
    # Look for a next link
    next_link = soup.select_one(
        "a[rel='next'], a.next, a:contains('Next'), a:contains('>')"
    )
    if not isinstance(next_link, Tag):
        # Try common text variants
        for a in soup.find_all("a"):
            if not isinstance(a, Tag):
                continue
            txt = a.get_text(strip=True).lower()
            if txt in {"next", ">", "older"}:
                next_link = a
                break
    if not isinstance(next_link, Tag):
        return template
    href = _get_attr_str(next_link, "href")
    if not href:
        return template
    if not href.startswith("http"):
        href = urlparse.urljoin(base_url + "/", href)

    # Normalize to path+query
    _, next_path_q = _split_base_and_path(href)
    # Replace seed query with token so we can compare schemata
    seed_enc = urlparse.quote_plus(seed_query)
    next_tpl = next_path_q.replace(seed_enc, "{query}")

    # If template already has {page}, keep it
    if "{page}" in template:
        return template

    # Query-string page parameter
    m = re.search(r"[?&]([a-zA-Z]{1,8})=(\d+)", next_tpl)
    if m:
        param = m.group(1)
        # Only add if this param isn't already present in template
        if f"{param}=" not in template:
            sep = "&" if "?" in template else "?"
            return f"{template}{sep}{param}={{page}}"

    # Path segment pagination (e.g., /search/{query}/1/)
    m2 = re.search(r"(/|%2F)(\d+)(/|%2F)?$", next_tpl)
    if m2 and not re.search(r"(/|%2F){\{page\}}", template):
        tail = "/{page}/" if template.endswith("/") else "/{page}/"
        return template.rstrip("/") + tail

    return template


def _pick_results_table(soup: BeautifulSoup) -> tuple[Optional[Tag], dict[str, Any]]:
    # Prefer tables that have seed/leech headers
    candidates = []
    for table in soup.select("table"):
        header_text = " ".join(
            th.get_text(" ", strip=True).lower() for th in table.select("th")
        )
        score = 0
        if "seed" in header_text:
            score += 2
        if "leech" in header_text:
            score += 2
        if "size" in header_text:
            score += 1
        if _get_attr_list(table, "class"):
            score += 1
        rows = table.select("tbody tr") or table.select("tr")
        if len(rows) >= 5:
            score += 1
        candidates.append((score, table))

    if not candidates:
        return None, {}
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], {}


def _css_for_table(table: Tag, soup: Optional[BeautifulSoup] = None) -> str:
    # Build a specific but stable selector for the table
    tid = _get_attr_str(table, "id")
    classes = _get_attr_list(table, "class")
    if tid:
        return f"table#{tid}"
    if classes:
        # Choose a minimal unique class among tables if possible
        if soup is not None:
            counts: list[tuple[int, str]] = []
            for c in classes:
                try:
                    count = len(soup.select(f"table.{c}"))
                except Exception:
                    count = 999
                counts.append((count, c))
            counts.sort(key=lambda x: (x[0], len(x[1])))
            chosen = counts[0][1]
            return f"table.{chosen}"
        # Fallback: prefer the first class
        return f"table.{classes[0]}"
    return "table"


def _infer_columns(table: Tag) -> dict[str, str]:
    """Infer per-column selectors by inspecting row cell classes and anchors.

    Prefers semantic TD classes (e.g., 'name', 'seeds', 'leeches', 'size', 'uploader')
    when present. Falls back to positional selectors only when necessary.
    """
    # Quickly detect semantic columns available anywhere in the table
    has_name_td = table.select_one("td.name") is not None
    has_seeds_td = table.select_one("td.seeds") is not None
    has_leeches_td = table.select_one("td.leeches, td.leech, td.leechers") is not None
    has_size_td = table.select_one("td.size") is not None
    has_uploader_td = table.select_one("td.uploader") is not None

    # Pick a representative data row (skip header rows using TH)
    rows = table.select("tbody tr") or table.select("tr")
    data_row: Optional[Tag] = None
    for r in rows:
        if r.find("td"):
            data_row = r
            break
    if not isinstance(data_row, Tag):
        return {}

    tds = list(data_row.select("td"))
    # Track indices for positional fallback
    seeds_idx = leech_idx = size_idx = uploader_idx = 0
    name_td: Optional[Tag] = None
    seeds_td = leech_td = size_td = uploader_td = None

    for i, td in enumerate(tds, start=1):
        classes = {c.lower() for c in _get_attr_list(td, "class")}
        if "name" in classes and name_td is None:
            name_td = td
        if ("seeds" in classes or not has_seeds_td) and seeds_td is None:
            seeds_td = td if "seeds" in classes else seeds_td
            seeds_idx = seeds_idx or (i if "seeds" in classes else 0)
        if (
            {"leeches", "leech", "leechers"} & classes or not has_leeches_td
        ) and leech_td is None:
            leech_td = td if ({"leeches", "leech", "leechers"} & classes) else leech_td
            leech_idx = leech_idx or (
                i if ({"leeches", "leech", "leechers"} & classes) else 0
            )
        if ("size" in classes or not has_size_td) and size_td is None:
            size_td = td if "size" in classes else size_td
            size_idx = size_idx or (i if "size" in classes else 0)
        if ("uploader" in classes or not has_uploader_td) and uploader_td is None:
            uploader_td = td if "uploader" in classes else uploader_td
            uploader_idx = uploader_idx or (i if "uploader" in classes else 0)

    def nth(idx: int) -> str:
        return f"td:nth-of-type({idx})" if idx else "td"

    # Build selectors for numeric/text columns
    seeders_selector = (
        "td.seeds" if has_seeds_td or seeds_td is not None else nth(seeds_idx)
    )
    leechers_selector = (
        "td.leeches" if has_leeches_td or leech_td is not None else nth(leech_idx)
    )
    size_selector = "td.size" if has_size_td or size_td is not None else nth(size_idx)
    uploader_selector = (
        "td.uploader a"
        if has_uploader_td or uploader_td is not None
        else (nth(uploader_idx) + " a")
        if uploader_idx
        else "td a[href*='user']"
    )

    # Name + magnet/detail selectors derived from the name cell
    name_selector = "td a"
    magnet_selector = "a[href^='magnet:']"
    if isinstance(name_td, Tag) or has_name_td:
        use_td = name_td if isinstance(name_td, Tag) else table.select_one("td.name")
        name_classes = (
            _get_attr_list(use_td, "class") if isinstance(use_td, Tag) else []
        )
        td_sel = (
            "td.name"
            if has_name_td or "name" in {c.lower() for c in name_classes}
            else ("td." + ".".join(name_classes) if name_classes else "td")
        )
        anchors = use_td.select("a") if isinstance(use_td, Tag) else []
        # Try to find the anchor linking to the detail page (e.g., '/torrent/...')
        title_idx = None
        for idx, a in enumerate(anchors, start=1):
            href = _get_attr_str(a, "href")
            if href.startswith("magnet:"):
                continue
            if href.startswith("/") and "/torrent" in href:
                title_idx = idx
                break
        if title_idx is None:
            # Fallback: choose the longest text anchor
            scored = [
                (idx, (a.get_text(strip=True) or ""))
                for idx, a in enumerate(anchors, start=1)
                if not _get_attr_str(a, "href").startswith("magnet:")
            ]
            if scored:
                title_idx = max(scored, key=lambda x: len(x[1]))[0]
        if title_idx is not None:
            name_selector = f"{td_sel} a:nth-of-type({title_idx})"
        # Magnet selector on results page may be a details link
        if isinstance(use_td, Tag) and use_td.select_one("a[href^='/torrent/']"):
            # Prefer a concise selector if the cell has the canonical 'name' class
            magnet_selector = (
                "td.name a[href^='/torrent/']"
                if td_sel.startswith("td.name")
                else f"{td_sel} a[href^='/torrent/']"
            )

    return {
        "name_selector": name_selector,
        "magnet_selector": magnet_selector,
        "seeders_selector": seeders_selector,
        "leechers_selector": leechers_selector,
        "size_selector": size_selector,
        "uploader_selector": uploader_selector,
    }


def _selector_for_cell_anchor(a: Tag) -> str:
    # Build a relative selector based on TD class/id or position
    td = a.find_parent("td")
    if isinstance(td, Tag):
        td_classes = _get_attr_list(td, "class")
        if td_classes:
            return f"td.{'.'.join(td_classes)} a"
        return "td a"
    return "a"


def _upgrade_template_with_placeholders(tpl: str) -> str:
    # Ensure placeholders for category and page exist for GenericTorrentScraper
    # If the template already includes query only, append category/page in a lenient way
    if "{category}" in tpl and "{page}" in tpl:
        return tpl
    # Handle common pattern '/{query}/<category-like>/<page-like>/'
    m = re.search(r"\{query\}/[^/]+/\d+/", tpl)
    if m:
        return re.sub(r"\{query\}/[^/]+/\d+/", "{query}/{category}/{page}/", tpl)
    # Handle '/{query}/<page-like>/'
    m2 = re.search(r"\{query\}/\d+/", tpl)
    if m2:
        return re.sub(r"\{query\}/\d+/", "{query}/{page}/", tpl)
    # Query-string based search: do not inject extra parameters; rely on template
    if "?" in tpl:
        return tpl
    # Path-based default
    tpl = tpl.rstrip("/")
    return f"{tpl}/{{category}}/{{page}}/"


def _get_attr_str(tag: Tag, attr: str) -> str:
    """Return an attribute value as a string, flattening lists safely.

    BeautifulSoup may return a list for some attributes (e.g., ``class``).
    This helper normalizes to a single string so callers can use string
    operations without mypy errors.
    """
    val = tag.get(attr)
    if isinstance(val, list):
        return " ".join([v for v in val if isinstance(v, str)])
    return val if isinstance(val, str) else ""


def _get_attr_list(tag: Tag, attr: str) -> list[str]:
    """Return an attribute as a list of strings.

    Handles values that may be a single string, a list of strings, or None.
    """
    val = tag.get(attr)
    if isinstance(val, list):
        return [v for v in val if isinstance(v, str)]
    if isinstance(val, str):
        return [v for v in val.split() if v]
    return []


def derive_site_name_from_base(base_url: str) -> str:
    """Derive a simple site label from the base URL netloc.

    Example: https://1337x.to -> 1337x
    """
    try:
        netloc = urlparse.urlparse(base_url).netloc
        host = netloc.split(":", 1)[0]
        root = host.split(".", 1)[0]
        return root.lower() or base_url
    except Exception:
        return base_url


def _split_base_and_path(url: str) -> tuple[str, str]:
    """Split a full URL into (base_url, path_with_query) parts.

    Example: https://example.org/search.php?q={query} ->
      (https://example.org, /search.php?q={query})
    """
    try:
        parsed = urlparse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return base, path
    except Exception:
        return "", url


def _slug_variants(term: str) -> list[str]:
    t = term.strip()
    variants = {t}
    tl = t.lower()
    variants.add(tl)
    # hyphen/underscore/plus-separated
    words = [w for w in re.split(r"\s+", tl) if w]
    if words:
        variants.add("-".join(words))
        variants.add("_".join(words))
        variants.add("+".join(words))
    # url-encoded forms
    variants.add(urlparse.quote(tl))
    variants.add(urlparse.quote_plus(tl))
    return list(variants)


def _derive_template_from_example(
    example_url: str, example_term: str
) -> tuple[str, str]:
    """Given a concrete search result URL and the term used to generate it,
    derive (base_url, search_path_template). Keeps {query} literal.
    """
    try:
        parsed = urlparse.urlparse(example_url)
    except Exception:
        return "", ""

    base = (
        f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    )
    # If no path, default to '/'
    path = parsed.path or "/"
    query = parsed.query or ""

    # Candidate forms of the example term to match
    variants = _slug_variants(example_term)

    # 1) Prefer query-string replacement when possible
    if query:
        try:
            pairs = urlparse.parse_qsl(query, keep_blank_values=True)
        except Exception:
            pairs = []
        # Try to find a param whose value matches a variant
        match_idx = -1
        for i, (k, v) in enumerate(pairs):
            val = v or ""
            if val in variants:
                match_idx = i
                break
        if match_idx >= 0:
            # Build a query string where only that value becomes {query}
            out_parts: list[str] = []
            for idx, (k, v) in enumerate(pairs):
                enc_k = urlparse.quote_plus(k)
                if idx == match_idx:
                    enc_v = "{query}"
                else:
                    enc_v = urlparse.quote_plus(v)
                out_parts.append(f"{enc_k}={enc_v}")
            new_query = "&".join(out_parts)
            path_q = path + ("?" + new_query if new_query else "")
            return base, path_q

    # 2) Fallback to path segment replacement
    segs = [s for s in path.split("/") if s != ""]
    # Identify a segment that matches a variant; else choose last segment
    seg_idx = -1
    for i, s in enumerate(segs):
        if s in variants:
            seg_idx = i
            break
    if seg_idx == -1 and segs:
        seg_idx = len(segs) - 1
    if seg_idx >= 0 and segs:
        segs[seg_idx] = "{query}"
        new_path = "/" + "/".join(segs)
    else:
        # No segments; simple '/{query}'
        new_path = "/{query}"

    # Reattach any constant query string
    if query:
        # Ensure braces not encoded; only encode constants
        try:
            pairs = urlparse.parse_qsl(query, keep_blank_values=True)
            out_parts = [
                f"{urlparse.quote_plus(k)}={urlparse.quote_plus(v)}" for k, v in pairs
            ]
            query_str = "&".join(out_parts)
        except Exception:
            query_str = query
        new_path = new_path + ("?" + query_str if query_str else "")

    return base, new_path


# ---------------------- config.ini updater ----------------------


def _yaml_safe_load_from_string(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data or {}
    except Exception:
        return {}


def _update_config_ini_with_site(
    site_name: str,
    base_url: str,
    search_path: str,
    movie_category: str,
    tv_category: str,
    config_path: Path | str = "config.ini",
    *,
    media_scopes: list[str] | None = None,
) -> None:
    """Insert/update the [search].websites JSON in config.ini with this site.

    Writes entries for both movies and tv using the derived search_url templates.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"config.ini not found at {path}")

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    # Locate [search] section
    search_start = None
    for i, line in enumerate(lines):
        if line.strip() == "[search]":
            search_start = i
            break

    if search_start is None:
        # Append minimal [search] with websites skeleton
        websites_obj: dict[str, Any] = {"movies": [], "tv": []}
        new_block = _dump_websites_block(websites_obj)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n[search]\n")
            fh.write(new_block)
        # Re-read to continue update
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.strip() == "[search]":
                search_start = i
                break

    # Guard: ensure search_start is now an int before proceeding
    if search_start is None:
        raise RuntimeError(
            "[config.ini] Could not locate [search] section after insertion."
        )

    # Find or insert 'websites ='
    websites_line = None
    next_key_or_section = None
    for i in range(search_start + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("[") and s.endswith("]"):
            next_key_or_section = i
            break
        if s.startswith("preferences") and "=" in s:
            next_key_or_section = i
            break
        if s.startswith("websites") and "=" in s:
            websites_line = i
            break

    if websites_line is None:
        insert_at = (
            next_key_or_section if next_key_or_section is not None else len(lines)
        )
        websites_obj = {"movies": [], "tv": []}
        new_block = _dump_websites_block(websites_obj)
        # Ensure it ends with newline and proper separation
        block_lines = [new_block if new_block.endswith("\n") else new_block + "\n"]
        lines[insert_at:insert_at] = block_lines
        path.write_text("".join(lines), encoding="utf-8")
        # Recurse to update the newly inserted block
        return _update_config_ini_with_site(
            site_name, base_url, search_path, movie_category, tv_category, config_path
        )

    # Determine end of websites block
    start = websites_line
    end = start + 1
    while end < len(lines):
        s = lines[end].strip()
        if s.startswith("[") and s.endswith("]"):
            break
        if s.startswith("preferences") and "=" in s:
            break
        end += 1

    # Parse JSON block
    first_line = lines[start]
    json_first = first_line.split("=", 1)[1].strip()
    block_str = json_first + "".join(lines[start + 1 : end])
    try:
        websites_obj = json.loads(block_str)
    except json.JSONDecodeError:
        websites_obj = {"movies": [], "tv": []}

    # Normalize structure
    movies_list: list[dict[str, Any]] = list(websites_obj.get("movies") or [])
    tv_list: list[dict[str, Any]] = list(websites_obj.get("tv") or [])

    # Build URLs
    def build_url(cat: str) -> str:
        try:
            # Provide only required placeholders; extras are ignored if not present
            url_tmpl = search_path
            # Keep '{query}' literal in the config so the app replaces it at runtime
            formatted = url_tmpl.format(query="{query}", category=cat, page=1)
            return urlparse.urljoin(base_url + "/", formatted)
        except Exception:
            # If format fails, at least ensure {query} placeholder is retained
            return urlparse.urljoin(
                base_url + "/", search_path.replace("{query}", "{query}")
            )

    movie_url = build_url(movie_category)
    tv_url = build_url(tv_category)

    def upsert(lst: list[dict[str, Any]], name: str, url: str) -> None:
        for item in lst:
            if isinstance(item, dict) and item.get("name") == name:
                item["search_url"] = url
                break
        else:
            lst.append({"name": name, "search_url": url})

    scopes = media_scopes or ["movies", "tv"]
    if "movies" in scopes:
        upsert(movies_list, site_name, movie_url)
    if "tv" in scopes:
        upsert(tv_list, site_name, tv_url)
    websites_obj["movies"] = movies_list
    websites_obj["tv"] = tv_list

    dumped = json.dumps(websites_obj, indent=4)
    new_block = _dump_websites_block_from_json(dumped)
    lines[start:end] = [new_block]
    path.write_text("".join(lines), encoding="utf-8")


def _dump_websites_block(obj: dict[str, Any]) -> str:
    dumped = json.dumps(obj, indent=4)
    return _dump_websites_block_from_json(dumped)


def _dump_websites_block_from_json(dumped: str) -> str:
    lines = dumped.splitlines()
    if not lines:
        return "websites = {}\n"
    first = lines[0]
    rest = "\n".join(lines[1:])
    return f"websites = {first}\n{rest}\n"


if __name__ == "__main__":
    main()
