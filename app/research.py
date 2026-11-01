"""Bounded public PubMed retrieval: no lifter details or model calls leave here."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import httpx

TTL_SECONDS = 86400
MAX_RESPONSE_BYTES = 524288
ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CATALOG = {
    "41843416": {"label": "ACSM resistance-training position stand (2026)",
                 "scope": "Evidence synthesis in healthy adults; general training guidance, not an individual prescription."},
    "40909191": {"label": "Resistance training during dietary weight loss (2025)",
                 "scope": "Adults with overweight or obesity during dietary weight loss; population findings may not apply to everyone."},
}
_lock = threading.Lock()


class ResearchUnavailable(Exception):
    pass


def _fetch(ids: list[str]) -> bytes:
    chunks = []
    size = 0
    with httpx.stream("GET", ENDPOINT, params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
                      timeout=6, follow_redirects=False) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ResearchUnavailable()
            chunks.append(chunk)
    return b"".join(chunks)


def _parse(payload: bytes, ids: list[str]) -> dict:
    if len(payload) > MAX_RESPONSE_BYTES or b"<!ENTITY" in payload.upper():
        raise ResearchUnavailable()
    records = {}
    for article in ElementTree.fromstring(payload).findall("PubmedArticle"):
        pmid = article.findtext("./MedlineCitation/PMID")
        if pmid not in ids:
            continue
        flags = [node.get("RefType", "").lower() for node in article.findall(".//CommentsCorrections")]
        types = ["".join(node.itertext()).lower() for node in article.findall(".//PublicationType")]
        if any("retract" in value or "concern" in value for value in flags + types):
            raise ResearchUnavailable()
        abstracts = article.findall(".//Abstract/AbstractText")
        conclusions = [node for node in abstracts if "conclu" in node.get("Label", "").lower()]
        if not abstracts:
            raise ResearchUnavailable()
        note = " ".join(" ".join(node.itertext()) for node in (conclusions or abstracts[-1:]))
        note = re.sub(r"\s+", " ", note).strip()[:800]
        if not note:
            raise ResearchUnavailable()
        records[pmid] = {"conclusion_excerpt": note}
    if set(records) != set(ids):
        raise ResearchUnavailable()
    return records


def routine_evidence(settings, goal: str, *, fetcher=None, now=None) -> list[dict]:
    """Reuse checked abstracts for a day. Failure stops a routine before paid inference."""
    ids = ["41843416"]
    if re.search(r"\b(drop|lose|loss|cut|fat|lean|lbs?|pounds?)\b", goal, re.I):
        ids.append("40909191")
    now = now or datetime.now(timezone.utc)
    path = Path(settings.research_cache_path)
    with _lock:
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        records = {}
        for pmid in ids:
            record = cache.get(pmid, {})
            if not isinstance(record, dict):
                continue
            try:
                age = (now - datetime.fromisoformat(record["checked_at"])).total_seconds()
                if not 0 <= age < TTL_SECONDS or not isinstance(record["conclusion_excerpt"], str) or not record["conclusion_excerpt"].strip():
                    continue
                records[pmid] = record
            except (KeyError, ValueError, TypeError):
                continue
        missing = [pmid for pmid in ids if pmid not in records]
        if missing:
            try:
                fresh = _parse((fetcher or _fetch)(missing), missing)
            except Exception:
                raise ResearchUnavailable("Public research could not be checked; no routine model call was made.") from None
            for pmid, record in fresh.items():
                records[pmid] = dict(record, checked_at=now.isoformat())
            cache.update(records)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                temp.write_text(json.dumps(cache))
                os.replace(temp, path)
            except OSError:
                pass  # Current verified records remain usable if cache storage is unavailable.
        return [{"pmid": pmid, "title": CATALOG[pmid]["label"],
                 "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                 "scope": CATALOG[pmid]["scope"],
                 "checked_at": records[pmid]["checked_at"],
                 "conclusion_excerpt": records[pmid]["conclusion_excerpt"][:800]} for pmid in ids]
