from __future__ import annotations

import csv
import html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


BASE_URL = "https://www.ablesci.com"
OPENALEX_API = "https://api.openalex.org/works"
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
STOPWORDS = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "via", "with", "using"}

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    source: str
    title: str
    doi: str = ""
    authors: str = ""
    year: str = ""
    venue: str = ""
    landing_url: str = ""
    pdf_url: str = ""
    confidence: float = 0.0
    message: str = ""


@dataclass
class MonitorResult:
    done: bool
    status: str
    message: str
    page_title: str = ""
    file_path: str = ""
    link_url: str = ""
    link_count: int = 0


@dataclass
class RobotConfig:
    enabled: bool = True
    ablesci_cookie: str = ""
    auto_publish: bool = True
    default_points: int = 30
    monitor_interval_seconds: int = 60
    max_hours: float = 24.0
    request_timeout_seconds: int = 30
    search_rows: int = 10
    open_pdf_confidence: float = 0.86
    download_dir: str = "data/literature_robot/downloads"
    status_log: str = "data/literature_robot/ablesci_monitor_log.csv"
    proxy: str = ""
    allowed_user_ids: set[str] = field(default_factory=set)
    allowed_group_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RobotConfig":
        data = data or {}
        return cls(
            enabled=as_bool(data.get("enabled"), True),
            ablesci_cookie=str(data.get("ablesci_cookie") or data.get("cookie") or ""),
            auto_publish=as_bool(data.get("auto_publish"), True),
            default_points=as_int(data.get("default_points"), 30, minimum=1),
            monitor_interval_seconds=as_int(data.get("monitor_interval_seconds"), 60, minimum=10),
            max_hours=as_float(data.get("max_hours"), 24.0, minimum=0.1),
            request_timeout_seconds=as_int(data.get("request_timeout_seconds"), 30, minimum=5),
            search_rows=as_int(data.get("search_rows"), 10, minimum=1),
            open_pdf_confidence=as_float(data.get("open_pdf_confidence"), 0.86, minimum=0.0, maximum=1.0),
            download_dir=str(data.get("download_dir") or "data/literature_robot/downloads"),
            status_log=str(data.get("status_log") or "data/literature_robot/ablesci_monitor_log.csv"),
            proxy=str(data.get("proxy") or ""),
            allowed_user_ids=split_values(data.get("allowed_user_ids")),
            allowed_group_ids=split_values(data.get("allowed_group_ids")),
        )

    @property
    def cookie(self) -> str:
        cookie = self.ablesci_cookie.lstrip("\ufeff").strip()
        if cookie:
            return cookie
        return os.environ.get("ABLESCI_COOKIE", "").lstrip("\ufeff").strip()

    @property
    def max_seconds(self) -> int:
        return max(1, int(self.max_hours * 3600))


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def as_float(
    value: Any,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def split_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in re.split(r"[\s,;，；]+", str(value)) if part.strip()}


def resolve_plugin_path(plugin_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = plugin_root / path
    return path


def request_headers(cookie: str, accept: str = "*/*") -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Cookie": cookie,
    }


def public_headers(accept: str = "*/*") -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }


def build_proxy_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({
            "http": proxy,
            "https": proxy,
        })
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def request_bytes(
    url: str, timeout: int, headers: dict[str, str] | None = None, proxy: str = ""
) -> tuple[bytes, str, str]:
    opener = build_proxy_opener(proxy)
    request = urllib.request.Request(url, headers=headers or public_headers())
    with opener.open(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", ""), response.geturl()


def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def title_tokens(title: str) -> set[str]:
    return {token for token in normalize_title(title).split() if len(token) > 1 and token not in STOPWORDS}


def title_confidence(query: str, candidate: str) -> float:
    sequence_score = SequenceMatcher(None, normalize_title(query), normalize_title(candidate)).ratio()
    query_tokens = title_tokens(query)
    candidate_tokens = title_tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return sequence_score
    overlap = query_tokens & candidate_tokens
    precision = len(overlap) / len(candidate_tokens)
    recall = len(overlap) / len(query_tokens)
    token_score = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return max(sequence_score, token_score)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def safe_filename(name: str, fallback: str = "paper.pdf") -> str:
    name = clean_text(name) or fallback
    name = re.sub(r"[<>:\"/\\|?*]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:180]


def post_form(url: str, data: dict[str, str], cookie: str, referer: str, timeout: int) -> str:
    headers = {
        **request_headers(cookie, "application/json, text/javascript, */*; q=0.01"),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
    }
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_text(url: str, cookie: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers=request_headers(cookie, "text/html,*/*"))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def csrf_from_page(page: str) -> str:
    patterns = [
        r'name="_csrf" value="([^"]+)"',
        r'<meta name="csrf-token" content="([^"]+)"',
        r"'_csrf': '([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, page)
        if match:
            return html.unescape(match.group(1))
    raise RuntimeError("Could not find CSRF token.")


def search_variants(title: str) -> list[str]:
    variants = [title]
    if ":" in title:
        variants.append(title.split(":", 1)[1])
    cleaned = re.sub(r"[^\w\s-]", " ", title, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    variants.append(cleaned)
    variants.append(" ".join(token for token in cleaned.split() if token.lower() not in STOPWORDS))
    if ":" in title:
        tail = re.sub(r"[^\w\s-]", " ", title.split(":", 1)[1], flags=re.UNICODE)
        tail = re.sub(r"\s+", " ", tail).strip()
        variants.append(tail)
        variants.append(" ".join(token for token in tail.split() if token.lower() not in STOPWORDS))
    return [variant for variant in dict.fromkeys(v.strip() for v in variants) if variant]


def parse_arxiv_id(entry_id: str) -> str:
    return entry_id.rstrip("/").split("/")[-1]


def find_arxiv(title: str, timeout: int, rows: int, proxy: str = "") -> Candidate | None:
    query = urllib.parse.urlencode(
        {"search_query": f'ti:"{title}"', "start": 0, "max_results": rows, "sortBy": "relevance"}
    )
    data, _, _ = request_bytes(f"{ARXIV_API}?{query}", timeout, public_headers("application/atom+xml,*/*"), proxy=proxy)
    root = ET.fromstring(data.decode("utf-8", errors="replace"))
    best = None
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_title = " ".join(entry.findtext("atom:title", default="", namespaces=ATOM_NS).split())
        arxiv_id = parse_arxiv_id(entry.findtext("atom:id", default="", namespaces=ATOM_NS))
        authors = []
        for author in entry.findall("atom:author", ATOM_NS)[:6]:
            name = author.findtext("atom:name", default="", namespaces=ATOM_NS).strip()
            if name:
                authors.append(name)
        candidate = Candidate(
            source="arxiv",
            title=entry_title,
            authors="; ".join(authors),
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            landing_url=f"https://arxiv.org/abs/{arxiv_id}",
            confidence=title_confidence(title, entry_title),
            message=f"arXiv:{arxiv_id}",
        )
        if best is None or candidate.confidence > best.confidence:
            best = candidate
    return best


def find_arxiv_html(title: str, timeout: int, rows: int, proxy: str = "") -> Candidate | None:
    best = None
    for variant in search_variants(title):
        query = urllib.parse.urlencode(
            {
                "query": variant,
                "searchtype": "all",
                "abstracts": "show",
                "order": "-announced_date_first",
                "size": 25 if rows <= 25 else 50,
            }
        )
        data, _, _ = request_bytes(f"https://arxiv.org/search/?{query}", timeout, public_headers("text/html,*/*"), proxy=proxy)
        page = data.decode("utf-8", errors="replace")
        for item in re.findall(
            r'<li class="arxiv-result">(.*?)</li>\s*(?=<li class="arxiv-result">|</ol>)',
            page,
            flags=re.S | re.I,
        ):
            id_match = re.search(r"https://arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", item)
            title_match = re.search(r'<p class="title is-5 mathjax">\s*(.*?)\s*</p>', item, flags=re.S | re.I)
            authors_match = re.search(r'<p class="authors">\s*(.*?)\s*</p>', item, flags=re.S | re.I)
            if not id_match or not title_match:
                continue
            arxiv_id = id_match.group(1)
            entry_title = clean_text(title_match.group(1))
            candidate = Candidate(
                source="arxiv_html",
                title=entry_title,
                authors=clean_text(authors_match.group(1)).replace("Authors:", "").strip() if authors_match else "",
                landing_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                confidence=max(0.0, min(1.0, title_confidence(title, entry_title) + 0.08)),
                message=f"arXiv:{arxiv_id}; query={variant}",
            )
            if best is None or candidate.confidence > best.confidence:
                best = candidate
        if best and best.confidence >= 0.86:
            return best
    return best


def normalize_doi(doi: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.I)


def find_openalex(title: str, timeout: int, rows: int, proxy: str = "") -> Candidate | None:
    query = urllib.parse.urlencode({"search": title, "per-page": rows})
    data, _, _ = request_bytes(f"{OPENALEX_API}?{query}", timeout, public_headers("application/json,*/*"), proxy=proxy)
    payload = json.loads(data.decode("utf-8", errors="replace"))
    best = None
    for work in payload.get("results", []):
        work_title = work.get("title") or ""
        score = title_confidence(title, work_title)
        locations = [work.get("best_oa_location"), work.get("primary_location")] + (work.get("locations") or [])
        pdf_url = ""
        landing_url = work.get("id") or ""
        for location in locations:
            if not location:
                continue
            pdf_url = pdf_url or location.get("pdf_url") or ""
            landing_url = location.get("landing_page_url") or landing_url
        oa = work.get("open_access") or {}
        if oa.get("is_oa"):
            score += 0.03
        if pdf_url:
            score += 0.05
        source = ((work.get("primary_location") or {}).get("source") or {})
        authors = []
        for authorship in work.get("authorships", [])[:6]:
            name = ((authorship.get("author") or {}).get("display_name") or "")
            if name:
                authors.append(name)
        candidate = Candidate(
            source="openalex",
            title=work_title,
            doi=normalize_doi(work.get("doi") or ""),
            authors="; ".join(authors),
            year=str(work.get("publication_year") or ""),
            venue=source.get("display_name", ""),
            landing_url=landing_url,
            pdf_url=pdf_url,
            confidence=max(0.0, min(1.0, score)),
            message=f"open_access={oa.get('is_oa')}",
        )
        if best is None or candidate.confidence > best.confidence:
            best = candidate
    return best


def find_crossref(title: str, timeout: int, rows: int, proxy: str = "") -> Candidate | None:
    query = urllib.parse.urlencode(
        {
            "query.title": title,
            "rows": rows,
            "select": "DOI,title,author,container-title,published-print,published-online,published,issued,URL,type",
        }
    )
    data, _, _ = request_bytes(f"https://api.crossref.org/works?{query}", timeout, public_headers("application/json,*/*"), proxy=proxy)
    items = json.loads(data.decode("utf-8", errors="replace")).get("message", {}).get("items", [])
    best = None
    for item in items:
        titles = item.get("title") or []
        item_title = titles[0] if titles else ""
        containers = item.get("container-title") or []
        authors = []
        for author in item.get("author", [])[:6]:
            name = " ".join(part for part in (author.get("given", ""), author.get("family", "")) if part).strip()
            if name:
                authors.append(name)
        year = ""
        for key in ("published-print", "published-online", "published", "issued"):
            parts = item.get(key, {}).get("date-parts")
            if parts and parts[0]:
                year = str(parts[0][0])
                break
        score = title_confidence(title, item_title)
        if containers:
            score += 0.03
        candidate = Candidate(
            source="crossref",
            title=item_title,
            doi=item.get("DOI", ""),
            authors="; ".join(authors),
            year=year,
            venue=containers[0] if containers else "",
            landing_url=item.get("URL", ""),
            confidence=max(0.0, min(1.0, score)),
            message="crossref",
        )
        if best is None or candidate.confidence > best.confidence:
            best = candidate
    return best


def best_candidate(title: str, timeout: int, rows: int, proxy: str = "") -> Candidate:
    candidates = []
    for finder in (find_arxiv, find_arxiv_html, find_openalex, find_crossref):
        try:
            candidate = finder(title, timeout, rows, proxy=proxy)
            if candidate:
                candidates.append(candidate)
        except Exception as exc:
            logger.warning("%s failed while resolving %r: %s", finder.__name__, title, exc)
    if not candidates:
        return Candidate(source="none", title=title, confidence=0.0, message="no candidates")
    candidates.sort(key=lambda c: (bool(c.pdf_url), c.confidence, c.source.startswith("arxiv")), reverse=True)
    return candidates[0]


def download_pdf(candidate: Candidate, out_dir: Path, timeout: int, overwrite: bool = False, proxy: str = "") -> tuple[str, str]:
    if not candidate.pdf_url:
        return "", "no_open_pdf_url"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / safe_filename(candidate.title, candidate.doi or "paper.pdf")
    if output.exists() and not overwrite:
        return str(output), "already_exists"
    data, content_type, final_url = request_bytes(candidate.pdf_url, timeout, public_headers("application/pdf,*/*"), proxy=proxy)
    if not (data.startswith(b"%PDF") or "application/pdf" in content_type.lower()):
        return "", f"not_pdf content_type={content_type} final_url={final_url}"
    output.write_bytes(data)
    return str(output), "downloaded"


def note_from_candidate(candidate: Candidate) -> str:
    lines = []
    if candidate.authors:
        lines.append(f"Authors: {candidate.authors}")
    if candidate.venue:
        lines.append(f"Journal/Conference: {candidate.venue}")
    if candidate.year:
        lines.append(f"Year: {candidate.year}")
    if candidate.doi:
        lines.append(f"DOI: {candidate.doi}")
    if candidate.landing_url:
        lines.append(f"URL: {candidate.landing_url}")
    return "\n".join(lines)


def publish_ablesci_request(candidate: Candidate, query_title: str, points: int, cookie: str, timeout: int) -> str:
    create_url = urllib.parse.urljoin(BASE_URL, "/assist/create")
    page = fetch_text(create_url, cookie, timeout)
    csrf = csrf_from_page(page)

    title = candidate.title or query_title
    doi = candidate.doi
    url = candidate.landing_url or (f"https://doi.org/{doi}" if doi else "")
    note = note_from_candidate(candidate)

    check_data = {
        "title": title,
        "doi": doi,
        "url": url,
        "point": str(points),
        "_csrf": csrf,
    }
    check_raw = post_form(
        urllib.parse.urljoin(BASE_URL, "/assist/check-before-assist-create"),
        check_data,
        cookie,
        create_url,
        timeout,
    )
    check = json.loads(check_raw)
    if check.get("code") != 0:
        raise RuntimeError(f"AbleSci pre-submit check failed: {check_raw[:1000]}")

    submit_data = {
        "_csrf": csrf,
        "onekey": "",
        "Assist[doi]": doi,
        "Assist[title]": title,
        "Assist[url]": url,
        "Assist[type]": "1",
        "Assist[point]": str(points),
        "Assist[note]": note,
        "Assist[remark]": "",
        "Assist[suppl]": "0",
        "Assist[close_at]": "",
    }
    raw = post_form(create_url, submit_data, cookie, create_url, timeout)
    result = json.loads(raw)
    if result.get("code") != 0:
        raise RuntimeError(f"AbleSci submit failed: {raw[:1000]}")
    detail = (result.get("data") or {}).get("url") or ""
    if detail.startswith("/"):
        detail = urllib.parse.urljoin(BASE_URL, detail)
    if not detail:
        raise RuntimeError(f"AbleSci submit succeeded but no detail URL was returned: {raw[:1000]}")
    return detail


def find_uploaded_file_ids(detail_url: str, cookie: str, timeout: int) -> list[str]:
    page = fetch_text(detail_url, cookie, timeout)
    ids = []
    for match in re.finditer(r'<input[^>]+class="assist-file-id"[^>]+value="([^"]+)"', page, flags=re.I):
        ids.append(html.unescape(match.group(1)))
    for match in re.finditer(r'<input[^>]+value="([^"]+)"[^>]+class="assist-file-id"', page, flags=re.I):
        ids.append(html.unescape(match.group(1)))
    return list(dict.fromkeys(ids))


def accept_file(detail_url: str, file_id: str, cookie: str, timeout: int) -> bool:
    page = fetch_text(detail_url, cookie, timeout)
    if f'value="{html.escape(file_id)}"' not in page and file_id not in page:
        return False
    csrf = csrf_from_page(page)
    raw = post_form(
        urllib.parse.urljoin(BASE_URL, "/assist/file-handle"),
        {"_csrf": csrf, "assist_file_id": file_id, "note": "", "type": "accept"},
        cookie,
        detail_url,
        timeout,
    )
    result = json.loads(raw)
    return result.get("code") == 0


def accept_available_files(detail_url: str, cookie: str, timeout: int) -> list[str]:
    accepted = []
    for file_id in find_uploaded_file_ids(detail_url, cookie, timeout):
        try:
            if accept_file(detail_url, file_id, cookie, timeout):
                accepted.append(file_id)
        except Exception as exc:
            logger.warning("Failed to accept AbleSci file %s from %s: %s", file_id, detail_url, exc)
    return accepted


def page_title(body: str) -> str:
    match = re.search(r"<title>(.*?)</title>", body, flags=re.S | re.I)
    return clean_text(match.group(1)) if match else ""


def extract_download_links(body: str, detail_url: str) -> list[dict[str, str]]:
    links = []
    for match in re.finditer(r"<a\b([^>]*)>(.*?)</a>", body, flags=re.S | re.I):
        attrs = match.group(1)
        text = clean_text(match.group(2))
        href_match = re.search(r"href=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        class_match = re.search(r"class=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        href = html.unescape(href_match.group(1)) if href_match else ""
        classes = class_match.group(1) if class_match else ""
        low = " ".join([href, classes, text]).lower()
        if not any(token in low for token in ("pdf", "download", "file", "下载", "文件", "附件")):
            continue
        if "javascript:void" in href.lower() or "assist-download-forbidden" in classes:
            continue
        links.append({"url": urllib.parse.urljoin(detail_url, href), "name": text or href.rsplit("/", 1)[-1]})
    unique = []
    seen = set()
    for link in links:
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        unique.append(link)
    unique.sort(key=lambda link: "/assist/download" in link["url"], reverse=True)
    return unique


def request_download_token(download_page_url: str, cookie: str, timeout: int) -> dict[str, Any]:
    page = fetch_text(download_page_url, cookie, timeout)
    csrf = csrf_from_page(page)
    id_match = re.search(r"/assist/download\?id=([^&\"']+)", download_page_url)
    server_match = re.search(r'file_server = "?([0-9]+)"?', page)
    if not id_match:
        raise RuntimeError("No AbleSci file id in download URL.")
    raw = post_form(
        urllib.parse.urljoin(BASE_URL, "/file/request-download-token"),
        {
            "_csrf": csrf,
            "highspeed": "1",
            "type": "assistFile",
            "id": id_match.group(1),
            "file_server": server_match.group(1) if server_match else "0",
        },
        cookie,
        download_page_url,
        timeout,
    )
    result = json.loads(raw)
    if result.get("code") != 0:
        raise RuntimeError(f"Token request failed: {raw[:1000]}")
    return result["data"]


def download_ablesci_link(url: str, name: str, cookie: str, out_dir: Path, timeout: int) -> tuple[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if "/assist/download" in url:
        token_data = request_download_token(url, cookie, timeout)
        filename = safe_filename(token_data.get("output_filename") or name)
        download_url = token_data["host"] + "?token=" + urllib.parse.quote(token_data["token"])
        data, content_type, final_url = request_bytes(
            download_url,
            max(timeout, 120),
            {**request_headers(cookie, "application/pdf,*/*"), "Referer": url},
        )
    else:
        filename = safe_filename(name)
        data, content_type, final_url = request_bytes(url, timeout, request_headers(cookie, "application/pdf,*/*"))
    if not (data.startswith(b"%PDF") or "application/pdf" in content_type.lower()):
        return "", f"not_pdf content_type={content_type} final_url={final_url}"
    output = out_dir / filename
    if output.exists():
        return str(output), "already_exists"
    output.write_bytes(data)
    return str(output), "downloaded"


def append_status_log(
    status_log: Path,
    detail_url: str,
    page: str,
    status: str,
    file_path: str,
    link_url: str,
    link_count: int,
) -> str:
    status_log.parent.mkdir(parents=True, exist_ok=True)
    exists = status_log.exists()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with status_log.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["time", "detail_url", "page_title", "status", "file_path", "link_url", "link_count"])
        writer.writerow([timestamp, detail_url, page, status, file_path, link_url, link_count])
    return f"{timestamp},{detail_url},{page},{status},{file_path},{link_url},links={link_count}"


CLOSED_KEYWORDS = {"已关闭", "closed", "已完结", "已结束", "已失效"}


def run_monitor_once(detail_url: str, cookie: str, out_dir: Path, status_log: Path, timeout: int) -> MonitorResult:
    body = fetch_text(detail_url, cookie, timeout)
    title = page_title(body)

    body_lower = body.lower()
    if any(kw in body_lower or kw in title.lower() for kw in CLOSED_KEYWORDS):
        message = append_status_log(status_log, detail_url, title, "closed", "", "", 0)
        return MonitorResult(
            done=True,
            status="closed",
            message="该求助已关闭，停止监控。",
            page_title=title,
            link_count=0,
        )

    links = extract_download_links(body, detail_url)
    message = append_status_log(status_log, detail_url, title, "waiting", "", "", len(links))
    for link in links:
        if "/assist/download" not in link["url"] and "pdf" not in link["url"].lower():
            continue
        file_path, status = download_ablesci_link(link["url"], link["name"], cookie, out_dir, timeout)
        message = append_status_log(status_log, detail_url, title, status, file_path, link["url"], len(links))
        if status in {"downloaded", "already_exists"}:
            return MonitorResult(
                done=True,
                status=status,
                message=message,
                page_title=title,
                file_path=file_path,
                link_url=link["url"],
                link_count=len(links),
            )
        if "/assist/download" in link["url"]:
            break
    return MonitorResult(
        done=False,
        status="waiting",
        message=message,
        page_title=title,
        link_count=len(links),
    )


def monitor_once(detail_url: str, cookie: str, out_dir: Path, status_log: Path, timeout: int) -> MonitorResult:
    accept_available_files(detail_url, cookie, timeout)
    return run_monitor_once(detail_url, cookie, out_dir, status_log, timeout)


def try_open_download(
    title: str,
    out_dir: Path,
    timeout: int,
    rows: int,
    confidence_threshold: float = 0.86,
    proxy: str = "",
) -> tuple[bool, Candidate, str, str]:
    candidate = best_candidate(title, timeout, rows, proxy=proxy)
    if candidate.pdf_url and candidate.confidence >= confidence_threshold:
        file_path, status = download_pdf(candidate, out_dir, timeout, overwrite=False, proxy=proxy)
        if status in {"downloaded", "already_exists"}:
            return True, candidate, file_path, status
        return False, candidate, "", status
    return False, candidate, "", "open_pdf_not_found"


def candidate_summary(candidate: Candidate) -> str:
    parts = [
        f"source={candidate.source}",
        f"confidence={candidate.confidence:.3f}",
    ]
    if candidate.title:
        parts.append(f"title={candidate.title}")
    if candidate.doi:
        parts.append(f"doi={candidate.doi}")
    if candidate.pdf_url:
        parts.append("pdf_url=yes")
    return "; ".join(parts)

