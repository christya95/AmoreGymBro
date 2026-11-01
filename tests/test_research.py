from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.research import ResearchUnavailable, routine_evidence

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def xml(ids):
    return ("<PubmedArticleSet>" + "".join(
        '<PubmedArticle><MedlineCitation><PMID>' + pmid + '</PMID><Article><Abstract>'
        '<AbstractText Label="CONCLUSIONS">Progressive resistance training can improve strength.</AbstractText>'
        '</Abstract></Article></MedlineCitation></PubmedArticle>' for pmid in ids
    ) + "</PubmedArticleSet>").encode()


def test_research_is_bounded_cached_and_goal_relevant(tmp_path):
    settings = SimpleNamespace(research_cache_path=str(tmp_path / "public.json"))
    requests = []
    def fetch(ids):
        requests.append(ids)
        return xml(ids)
    first = routine_evidence(settings, "Drop to 170 lb while stronger", fetcher=fetch, now=NOW)
    assert requests == [["41843416", "40909191"]]
    assert len(first) == 2
    assert "170" not in (tmp_path / "public.json").read_text()
    assert routine_evidence(settings, "Drop to 170 lb", fetcher=fetch, now=NOW + timedelta(hours=23)) == first
    assert len(requests) == 1
    refreshed = routine_evidence(settings, "Get stronger", fetcher=fetch, now=NOW + timedelta(days=1))
    assert requests[-1] == ["41843416"]
    assert len(refreshed) == 1
    assert all(len(item["conclusion_excerpt"]) <= 800 for item in first)


@pytest.mark.parametrize("response", [
    b"not XML", xml(["99999999"]), b'<PubmedArticleSet/>',
    xml(["41843416"]).replace(b'<Article>', b'<CommentsCorrections RefType="RetractionIn"/><Article>'),
    b"x" * 524289,
])
def test_invalid_or_retracted_evidence_is_not_used(tmp_path, response):
    settings = SimpleNamespace(research_cache_path=str(tmp_path / "public.json"))
    with pytest.raises(ResearchUnavailable):
        routine_evidence(settings, "Strength", fetcher=lambda ids: response, now=NOW)


def test_expired_cache_does_not_claim_fresh_research_when_offline(tmp_path):
    settings = SimpleNamespace(research_cache_path=str(tmp_path / "public.json"))
    routine_evidence(settings, "Strength", fetcher=xml, now=NOW)
    def offline(ids):
        raise OSError("offline")
    with pytest.raises(ResearchUnavailable):
        routine_evidence(settings, "Strength", fetcher=offline, now=NOW + timedelta(days=2))
