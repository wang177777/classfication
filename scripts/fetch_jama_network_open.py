#!/usr/bin/env python3
"""Harvest JAMA Network Open records and concise research-design summaries.

Sources:
- JAMA Network Open monthly issue pages (publisher titles, article cards, teaser sentences)
- Crossref REST API (DOIs, dates, authors, metadata)
- Europe PMC REST API (PMID/PMCID, abstracts, publication types)

The script intentionally outputs both a full index and a research-only table.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

START_DATE = os.getenv("START_DATE", "2026-01-24")
END_DATE = os.getenv("END_DATE", "2026-07-24")
OUT_DIR = Path(os.getenv("OUT_DIR", "jama_output"))
ISSN = "2574-3805"
JOURNAL_ABBR = 'JAMA Netw Open'
USER_AGENT = os.getenv(
    "USER_AGENT",
    "JAMA-six-month-audit/1.0 (literature metadata reconciliation; contact: github-actions)",
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

NON_RESEARCH_TITLE_RE = re.compile(
    r"^(?:error(?:s)?\b|correction\b|notice of retraction\b|retraction\b|"
    r"expression of concern\b|jama network open$|author interview\b|"
    r"reply\b|in reply\b)",
    re.I,
)
NON_RESEARCH_TYPES = {
    "Invited Commentary",
    "Editorial",
    "Viewpoint",
    "Comment & Response",
    "Correction",
    "JAMA Network Open Masthead",
    "Editor's Note",
    "Letter",
    "News",
}

DESIGN_PATTERNS: list[tuple[str, str]] = [
    (r"systematic review and (?:network )?meta-analysis", "系统综述与Meta分析"),
    (r"network meta-analysis", "网状Meta分析"),
    (r"systematic review", "系统综述"),
    (r"meta-analysis", "Meta分析"),
    (r"stepped-wedge cluster-randomized clinical trial", "阶梯楔形整群随机临床试验"),
    (r"cluster randomized crossover trial", "整群随机交叉试验"),
    (r"cluster-randomized clinical trial", "整群随机临床试验"),
    (r"cluster randomized trial", "整群随机试验"),
    (r"randomized crossover trial", "随机交叉试验"),
    (r"crossover trial", "交叉试验"),
    (r"randomized clinical trial", "随机临床试验"),
    (r"randomized trial", "随机试验"),
    (r"nonrandomized clinical trial", "非随机临床试验"),
    (r"clinical trial", "临床试验"),
    (r"case-control study", "病例对照研究"),
    (r"cohort study", "队列研究"),
    (r"repeated cross-sectional study", "重复横断面研究"),
    (r"cross-sectional study", "横断面研究"),
    (r"survey study", "调查研究"),
    (r"qualitative study", "定性研究"),
    (r"mixed-methods study", "混合方法研究"),
    (r"diagnostic study", "诊断研究"),
    (r"prognostic study", "预后研究"),
    (r"ecological study", "生态学研究"),
    (r"economic evaluation", "卫生经济学评价"),
    (r"cost-effectiveness analysis", "成本效果分析"),
    (r"decision analytical model", "决策分析模型"),
    (r"decision analytic model", "决策分析模型"),
    (r"modeling study", "建模研究"),
    (r"quality improvement study", "质量改进研究"),
    (r"secondary analysis", "二次分析"),
    (r"post hoc analysis", "事后分析"),
    (r"genetic association study", "遗传关联研究"),
    (r"validation study", "验证研究"),
]

ABSTRACT_LABELS = [
    "IMPORTANCE",
    "QUESTION",
    "OBJECTIVE",
    "OBJECTIVES",
    "PURPOSE",
    "DESIGN, SETTING, AND PARTICIPANTS",
    "DESIGN AND PARTICIPANTS",
    "DESIGN",
    "SETTING",
    "PARTICIPANTS",
    "EXPOSURES",
    "EXPOSURE",
    "INTERVENTIONS",
    "INTERVENTION",
    "MAIN OUTCOMES AND MEASURES",
    "OUTCOMES AND MEASURES",
    "MAIN OUTCOME AND MEASURE",
    "RESULTS",
    "CONCLUSIONS AND RELEVANCE",
    "CONCLUSIONS",
]


def request(url: str, *, params: dict[str, Any] | None = None, timeout: int = 90) -> requests.Response:
    last: Exception | None = None
    for attempt in range(6):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(f"retryable status {response.status_code}", response=response)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 5:
                break
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"Request failed: {url} params={params}: {last}")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_doi(value: Any) -> str:
    doi = clean_text(value).lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    return doi.strip().rstrip(".,;")


def normalize_title(value: Any) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def parse_date_parts(obj: dict[str, Any], keys: Iterable[str]) -> str:
    candidates: list[date] = []
    for key in keys:
        parts = (((obj.get(key) or {}).get("date-parts") or [[]])[0])
        if not parts:
            continue
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            candidates.append(date(year, month, day))
        except (ValueError, TypeError):
            continue
    return min(candidates).isoformat() if candidates else ""


def parse_abstract_sections(abstract: str) -> dict[str, str]:
    text = clean_text(abstract)
    if not text:
        return {}
    label_alt = "|".join(sorted((re.escape(x) for x in ABSTRACT_LABELS), key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Z])({label_alt})\s*:?[ ]*", re.I)
    matches = list(pattern.finditer(text))
    if not matches:
        return {"UNSTRUCTURED": text}
    result: dict[str, str] = {}
    for idx, match in enumerate(matches):
        label = match.group(1).upper()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        value = text[start:end].strip(" .;:")
        if value:
            result[label] = value
    return result


def first_sentences(text: str, n: int = 2, max_chars: int = 900) -> str:
    text = clean_text(text)
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    result = " ".join(sentences[:n]).strip()
    return result[:max_chars].rstrip()


def design_from_text(*texts: str) -> tuple[str, str]:
    joined = " ".join(clean_text(x) for x in texts if x)
    for pattern, zh in DESIGN_PATTERNS:
        match = re.search(pattern, joined, re.I)
        if match:
            return clean_text(match.group(0)), zh
    return "", ""


def authors_crossref(item: dict[str, Any]) -> str:
    authors: list[str] = []
    for author in item.get("author") or []:
        name = " ".join(x for x in [clean_text(author.get("given")), clean_text(author.get("family"))] if x)
        if name:
            authors.append(name)
    return "; ".join(authors)


def fetch_crossref() -> list[dict[str, Any]]:
    url = f"https://api.crossref.org/journals/{ISSN}/works"
    cursor = "*"
    rows = 1000
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        params = {
            "filter": f"from-pub-date:{START_DATE},until-pub-date:{END_DATE},type:journal-article",
            "rows": rows,
            "cursor": cursor,
            "cursor-max": rows,
            "sort": "published",
            "order": "asc",
        }
        message = request(url, params=params).json()["message"]
        items = message.get("items") or []
        for item in items:
            doi = normalize_doi(item.get("DOI"))
            if not doi or doi in seen:
                continue
            seen.add(doi)
            records.append(
                {
                    "doi": doi,
                    "title": clean_text((item.get("title") or [""])[0]),
                    "authors": authors_crossref(item),
                    "publication_date": parse_date_parts(
                        item,
                        ["published-online", "published-print", "published", "issued", "created"],
                    ),
                    "abstract": clean_text(item.get("abstract")),
                    "crossref_type": clean_text(item.get("type")),
                    "crossref_subtype": clean_text(item.get("subtype")),
                    "volume": clean_text(item.get("volume")),
                    "issue": clean_text(item.get("issue")),
                    "article_url": first_nonempty(item.get("URL")),
                    "publisher": clean_text(item.get("publisher")),
                    "source_crossref": True,
                }
            )
        next_cursor = message.get("next-cursor")
        if len(items) < rows or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return records


def fetch_europe_pmc() -> list[dict[str, Any]]:
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    cursor = "*"
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    while True:
        params = {
            "query": f'JOURNAL:"{JOURNAL_ABBR}" AND FIRST_PDATE:[{START_DATE} TO {END_DATE}]',
            "format": "json",
            "resultType": "core",
            "pageSize": 1000,
            "cursorMark": cursor,
        }
        payload = request(url, params=params).json()
        results = (payload.get("resultList") or {}).get("result") or []
        for item in results:
            doi = normalize_doi(item.get("doi"))
            pmid = clean_text(item.get("pmid") or (item.get("id") if item.get("source") == "MED" else ""))
            key = (doi, pmid)
            if key in seen:
                continue
            seen.add(key)
            pubtypes = (item.get("pubTypeList") or {}).get("pubType") or []
            if isinstance(pubtypes, str):
                pubtypes = [pubtypes]
            journal_info = item.get("journalInfo") or {}
            records.append(
                {
                    "doi": doi,
                    "title": clean_text(item.get("title")),
                    "authors": clean_text(item.get("authorString")),
                    "publication_date": clean_text(item.get("firstPublicationDate")),
                    "abstract": clean_text(item.get("abstractText")),
                    "pmid": pmid,
                    "pmcid": clean_text(item.get("pmcid")),
                    "pub_types": "; ".join(clean_text(x) for x in pubtypes if clean_text(x)),
                    "volume": clean_text(journal_info.get("volume")),
                    "issue": clean_text(journal_info.get("issue")),
                    "source_europe_pmc": True,
                }
            )
        next_cursor = payload.get("nextCursorMark")
        if len(results) < 1000 or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return records


def plausible_title(text: str) -> bool:
    if not text or len(text) < 8 or len(text) > 350:
        return False
    lowered = text.lower()
    bad = {
        "abstract",
        "full text",
        "pdf",
        "cme & moc",
        "jama network open",
        "original investigation",
        "research letter",
        "invited commentary",
        "editorial",
        "correction",
    }
    if lowered in bad or lowered.startswith("jama netw open."):
        return False
    if lowered.startswith("this "):
        return False
    return True


def nearest_article_container(node: Any) -> Any:
    for parent in node.parents:
        try:
            text = clean_text(parent.get_text(" ", strip=True))
            links = parent.find_all("a", href=True)
        except Exception:  # noqa: BLE001
            continue
        full_links = [a for a in links if "/fullarticle/" in (a.get("href") or "")]
        if full_links and 100 <= len(text) <= 6000:
            return parent
    return node.parent


def prior_label(tag: Any, candidates: set[str], max_steps: int = 120) -> str:
    steps = 0
    for previous in tag.find_all_previous(string=True):
        steps += 1
        if steps > max_steps:
            break
        text = clean_text(previous)
        if text in candidates:
            return text
    return ""


def parse_issue_page(issue_num: int) -> list[dict[str, Any]]:
    url = f"https://jamanetwork.com/journals/jamanetworkopen/issue/9/{issue_num}"
    response = request(url)
    soup = BeautifulSoup(response.text, "html.parser")
    records_by_doi: dict[str, dict[str, Any]] = {}

    doi_nodes = soup.find_all(string=re.compile(r"doi\s*:\s*10\.1001/jamanetworkopen\.", re.I))
    for doi_node in doi_nodes:
        citation_text = clean_text(doi_node)
        match = re.search(r"doi\s*:\s*(10\.1001/jamanetworkopen\.\d{4}\.\d+)", citation_text, re.I)
        if not match:
            continue
        doi = normalize_doi(match.group(1))
        container = nearest_article_container(doi_node)
        links = container.find_all("a", href=True) if container else []
        title_link_candidates: list[tuple[int, Any]] = []
        for link in links:
            href = link.get("href") or ""
            text = clean_text(link.get_text(" ", strip=True))
            if "/fullarticle/" in href and plausible_title(text):
                title_link_candidates.append((len(text), link))
        title_link = max(title_link_candidates, default=(0, None), key=lambda x: x[0])[1]
        title = clean_text(title_link.get_text(" ", strip=True)) if title_link else ""
        article_url = urljoin(url, title_link.get("href")) if title_link else ""

        teaser = ""
        if container:
            for paragraph in container.find_all(["p", "div", "span"]):
                text = clean_text(paragraph.get_text(" ", strip=True))
                if text.startswith("This ") and 40 <= len(text) <= 1800:
                    teaser = text
                    break

        article_types = {
            "Original Investigation",
            "Research Letter",
            "Systematic Review",
            "Meta-analysis",
            "Special Communication",
            "Invited Commentary",
            "Editorial",
            "Viewpoint",
            "Comment & Response",
            "Correction",
            "JAMA Network Open Masthead",
        }
        article_type = prior_label(doi_node.parent, article_types)
        specialty = ""
        for prev in doi_node.parent.find_all_previous(["h3", "h4", "h5"]):
            text = clean_text(prev.get_text(" ", strip=True))
            if text.isupper() and 3 <= len(text) <= 80:
                specialty = text.title()
                break

        records_by_doi[doi] = {
            "doi": doi,
            "title": title,
            "research_idea_en": teaser,
            "article_url": article_url,
            "article_type": article_type,
            "specialty": specialty,
            "issue_month": issue_num,
            "issue_url": url,
            "source_jama_issue": True,
        }

    # Text fallback catches cards whose HTML ancestry differs.
    lines = [clean_text(x) for x in soup.get_text("\n").splitlines()]
    lines = [x for x in lines if x]
    citation_indices = [
        i for i, line in enumerate(lines)
        if re.search(r"JAMA Netw Open\.\s*2026;9\(\d+\):.*doi:10\.1001/jamanetworkopen\.", line, re.I)
    ]
    previous_index = 0
    for index in citation_indices:
        citation = lines[index]
        match = re.search(r"doi\s*:\s*(10\.1001/jamanetworkopen\.\d{4}\.\d+)", citation, re.I)
        if not match:
            previous_index = index + 1
            continue
        doi = normalize_doi(match.group(1))
        block = lines[max(previous_index, index - 24):index]
        previous_index = index + 1
        teaser = next((x for x in reversed(block) if x.startswith("This ") and len(x) >= 40), "")
        title = ""
        for candidate in reversed(block):
            if plausible_title(candidate) and not re.search(r"\b(?:MD|PhD|MPH|MSc|BS|BA|et al)\b", candidate):
                title = candidate
                break
        record = records_by_doi.setdefault(
            doi,
            {
                "doi": doi,
                "issue_month": issue_num,
                "issue_url": url,
                "source_jama_issue": True,
            },
        )
        if not record.get("title") and title:
            record["title"] = title
        if not record.get("research_idea_en") and teaser:
            record["research_idea_en"] = teaser

    return list(records_by_doi.values())


def fetch_jama_issues() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue_num in range(1, 8):
        try:
            records.extend(parse_issue_page(issue_num))
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING issue {issue_num} failed: {exc}", file=sys.stderr)
    dedup: dict[str, dict[str, Any]] = {}
    for record in records:
        doi = normalize_doi(record.get("doi"))
        if doi:
            dedup[doi] = {**dedup.get(doi, {}), **record, "doi": doi}
    return list(dedup.values())


def merge_records(
    crossref: list[dict[str, Any]],
    epmc: list[dict[str, Any]],
    jama: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    title_index: dict[str, str] = {}

    def key_for(record: dict[str, Any]) -> str:
        doi = normalize_doi(record.get("doi"))
        if doi:
            return f"doi:{doi}"
        pmid = clean_text(record.get("pmid"))
        if pmid:
            return f"pmid:{pmid}"
        title = normalize_title(record.get("title"))
        return f"title:{title}"

    def upsert(record: dict[str, Any]) -> None:
        key = key_for(record)
        if key.endswith(":"):
            return
        if key.startswith("title:") and key in title_index:
            key = title_index[key]
        base = merged.setdefault(key, {})
        for field, value in record.items():
            if isinstance(value, bool):
                base[field] = bool(base.get(field)) or value
            elif clean_text(value):
                if field == "abstract" and len(clean_text(value)) < len(clean_text(base.get(field))):
                    continue
                if field not in base or not clean_text(base.get(field)):
                    base[field] = value
                elif field in {"research_idea_en", "article_type", "specialty", "article_url"} and record.get("source_jama_issue"):
                    base[field] = value
        title = normalize_title(base.get("title"))
        if title:
            title_index[f"title:{title}"] = key

    for record in crossref:
        upsert(record)
    for record in epmc:
        upsert(record)
    for record in jama:
        doi_key = f"doi:{normalize_doi(record.get('doi'))}" if record.get("doi") else ""
        if doi_key and doi_key not in merged:
            issue_month = int(record.get("issue_month") or 0)
            # January-only publisher records cannot be safely day-filtered without an index date.
            if issue_month == 1:
                continue
        upsert(record)

    output: list[dict[str, Any]] = []
    for record in merged.values():
        record["doi"] = normalize_doi(record.get("doi"))
        sections = parse_abstract_sections(clean_text(record.get("abstract")))
        objective = first_nonempty(
            sections.get("OBJECTIVE"),
            sections.get("OBJECTIVES"),
            sections.get("PURPOSE"),
            sections.get("QUESTION"),
        )
        design_setting = first_nonempty(
            sections.get("DESIGN, SETTING, AND PARTICIPANTS"),
            sections.get("DESIGN AND PARTICIPANTS"),
            sections.get("DESIGN"),
        )
        population = first_nonempty(sections.get("PARTICIPANTS"), design_setting)
        intervention = first_nonempty(
            sections.get("INTERVENTIONS"),
            sections.get("INTERVENTION"),
            sections.get("EXPOSURES"),
            sections.get("EXPOSURE"),
        )
        outcome = first_nonempty(
            sections.get("MAIN OUTCOMES AND MEASURES"),
            sections.get("OUTCOMES AND MEASURES"),
            sections.get("MAIN OUTCOME AND MEASURE"),
        )
        idea = clean_text(record.get("research_idea_en"))
        if not idea:
            if objective and design_setting:
                idea = f"Objective: {first_sentences(objective, 1)} Design/data: {first_sentences(design_setting, 1)}"
            elif objective:
                idea = first_sentences(objective, 2)
            elif sections.get("UNSTRUCTURED"):
                idea = first_sentences(sections["UNSTRUCTURED"], 2)
        record["research_idea_en"] = idea
        record["objective_en"] = objective
        record["population_data_en"] = population
        record["intervention_exposure_en"] = intervention
        record["outcome_en"] = outcome
        design_en, design_zh = design_from_text(
            idea,
            design_setting,
            clean_text(record.get("pub_types")),
            clean_text(record.get("title")),
        )
        record["study_design_en"] = design_en
        record["study_design_zh"] = design_zh

        title = clean_text(record.get("title"))
        article_type = clean_text(record.get("article_type"))
        excluded_reason = ""
        is_research = False
        if NON_RESEARCH_TITLE_RE.search(title):
            excluded_reason = "题名显示为勘误、撤稿、回复或刊头等非研究内容"
        elif article_type in NON_RESEARCH_TYPES:
            excluded_reason = f"文章类型为{article_type}，不是原始研究"
        elif idea and (design_en or re.search(r"\b(?:study|trial|review|analysis|evaluation|model)\b", idea, re.I)):
            is_research = True
        elif clean_text(record.get("abstract")) and not re.search(
            r"\b(?:editorial|comment|letter|news|biography)\b",
            clean_text(record.get("pub_types")),
            re.I,
        ):
            is_research = True
        else:
            excluded_reason = "缺少可核验的研究设计/摘要信息，暂列为非研究或待核验"
        record["is_research"] = is_research
        record["excluded_reason"] = excluded_reason

        # Conservative date-range check. JAMA-only Feb-Jul entries are retained with month precision.
        pub_date = clean_text(record.get("publication_date"))
        if pub_date:
            try:
                parsed = datetime.fromisoformat(pub_date[:10]).date()
                record["within_date_window"] = date.fromisoformat(START_DATE) <= parsed <= date.fromisoformat(END_DATE)
            except ValueError:
                record["within_date_window"] = True
        else:
            issue_month = int(record.get("issue_month") or 0)
            record["publication_date"] = f"2026-{issue_month:02d}" if issue_month else ""
            record["within_date_window"] = issue_month in {2, 3, 4, 5, 6, 7}

        source_labels = []
        if record.get("source_jama_issue"):
            source_labels.append("JAMA issue page")
        if record.get("source_crossref"):
            source_labels.append("Crossref")
        if record.get("source_europe_pmc"):
            source_labels.append("Europe PMC")
        record["source_coverage"] = "; ".join(source_labels)
        record["article_url"] = first_nonempty(
            record.get("article_url"),
            f"https://doi.org/{record['doi']}" if record.get("doi") else "",
        )
        output.append(record)

    output = [r for r in output if r.get("within_date_window")]
    output.sort(key=lambda r: (clean_text(r.get("publication_date")), clean_text(r.get("title"))))
    return output


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    crossref = fetch_crossref()
    epmc = fetch_europe_pmc()
    jama = fetch_jama_issues()
    merged = merge_records(crossref, epmc, jama)
    research = [r for r in merged if r.get("is_research")]

    common_columns = [
        "publication_date",
        "article_type",
        "specialty",
        "study_design_zh",
        "study_design_en",
        "title",
        "authors",
        "doi",
        "pmid",
        "pmcid",
        "research_idea_en",
        "objective_en",
        "population_data_en",
        "intervention_exposure_en",
        "outcome_en",
        "pub_types",
        "volume",
        "issue",
        "source_coverage",
        "article_url",
        "issue_url",
    ]
    write_csv(OUT_DIR / "research_papers.csv", research, common_columns)
    write_csv(
        OUT_DIR / "all_records.csv",
        merged,
        ["is_research", "excluded_reason", *common_columns, "within_date_window"],
    )

    summary = {
        "generated_at_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "date_window": {"start": START_DATE, "end": END_DATE, "inclusive": True},
        "scope_note": (
            "Full index retains research and nonresearch publication records. Research table includes records "
            "with a verifiable study design/abstract and excludes commentaries, editorials, corrections, "
            "retractions, masthead material, and similar nonresearch content."
        ),
        "counts": {
            "crossref_records": len(crossref),
            "europe_pmc_records": len(epmc),
            "jama_issue_records": len(jama),
            "merged_full_index": len(merged),
            "research_records": len(research),
            "nonresearch_or_pending": len(merged) - len(research),
            "research_missing_idea": sum(not clean_text(r.get("research_idea_en")) for r in research),
            "research_missing_doi": sum(not clean_text(r.get("doi")) for r in research),
            "research_missing_exact_day": sum(len(clean_text(r.get("publication_date"))) < 10 for r in research),
        },
        "research_design_counts": Counter(clean_text(r.get("study_design_zh")) or "未自动识别" for r in research),
        "source_coverage_counts": Counter(clean_text(r.get("source_coverage")) for r in merged),
    }
    with (OUT_DIR / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)

    # Small human-readable preview for quick connector inspection.
    preview = research[:25]
    write_csv(
        OUT_DIR / "preview_25.csv",
        preview,
        ["publication_date", "study_design_zh", "title", "research_idea_en", "doi", "article_url"],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=dict))


if __name__ == "__main__":
    main()
