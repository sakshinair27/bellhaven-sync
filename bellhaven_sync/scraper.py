"""Scrape every Bellhaven community from the public website.

The directory at /communities is paginated, but it is not the only source of truth:
the homepage announces newly-added communities that are not (yet) in the directory.
So we crawl: start from the homepage and the directory, follow pagination, and collect
every /communities/<slug> link we see anywhere. Each detail page is then parsed.
"""
import html
import re
import urllib.parse

from . import config
from .http import request

DETAIL_RE = re.compile(r'href="(/communities/[a-z0-9\-]+)"')
PAGE_RE = re.compile(r'href="(/communities\?page=\d+)"')
CLAIMED_RE = re.compile(r"serve\s+(\d+)\s+communities", re.I)


def _get(path):
    return request("GET", urllib.parse.urljoin(config.BASE_URL, path))[1]


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_detail(slug_path, page):
    name = _text(re.search(r"<h1>(.*?)</h1>", page, re.S).group(1))
    fields = dict(
        (_text(k).lower(), v)
        for k, v in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", page, re.S)
    )
    addr_lines = [_text(x) for x in re.split(r"<br\s*/?>", fields.get("address", ""))]
    street = addr_lines[0] if addr_lines else ""
    m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", addr_lines[-1] if addr_lines else "")
    city, state, zip_ = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
    care = [_text(b) for b in re.findall(r'<span class="badge">(.*?)</span>', fields.get("care offerings", ""), re.S)]
    return {
        "slug": slug_path.rsplit("/", 1)[-1],
        "url": urllib.parse.urljoin(config.BASE_URL, slug_path),
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_,
        "care_offerings": care,
        "administrator": _text(fields.get("administrator", "")),
        "phone": _text(fields.get("phone", "")),
    }


def scrape():
    """Returns (locations, meta). meta carries crawl diagnostics for the run log."""
    seen_pages, queue, details = set(), ["/", "/communities"], {}
    discovered_via = {}
    claimed = None
    while queue:
        path = queue.pop(0)
        if path in seen_pages:
            continue
        seen_pages.add(path)
        page = _get(path)
        if claimed is None and (m := CLAIMED_RE.search(page)):
            claimed = int(m.group(1))
        for p in PAGE_RE.findall(page):
            if p not in seen_pages:
                queue.append(p)
        for d in DETAIL_RE.findall(page):
            details.setdefault(d, None)
            discovered_via.setdefault(d, path)

    locations = []
    for path in sorted(details):
        loc = parse_detail(path, _get(path))
        loc["discovered_via"] = discovered_via[path]
        loc["in_directory"] = discovered_via[path].startswith("/communities")
        locations.append(loc)

    meta = {
        "pages_crawled": sorted(seen_pages),
        "locations_found": len(locations),
        "homepage_claimed_count": claimed,
        "not_in_directory": [l["name"] for l in locations if not l["in_directory"]],
    }
    return locations, meta
