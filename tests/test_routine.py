from fastapi.testclient import TestClient
import pytest

from tests.test_app import signed_in


@pytest.fixture(autouse=True)
def public_research_fixture(monkeypatch):
    monkeypatch.setattr("app.routine.routine_evidence", lambda *_args: [{
        "title": "Public training review", "url": "https://pubmed.ncbi.nlm.nih.gov/41843416/",
        "checked_at": "2026-11-01T00:00:00+00:00", "scope": "Healthy adults",
        "conclusion_excerpt": "Progressive resistance training improves strength.",
    }])


class CountingModel:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self


def _seed_complete(app, *, height_cm=180.0):
    database = app.state.db
    database.save_goal("Drop to 170 lb while getting stronger progressively.", "2026-10-31T18:00:00Z")
    database.upsert_body_measurement({"date": "2026-10-21", "weight_kg": 84.0}, "2026-10-31T18:00:00Z")
    database.save_body_entry(
        height_cm=height_cm,
        age_years=35,
        sex="male",
        activity="moderate",
        updated_at="2026-10-31T18:00:00Z",
        days_per_week=4,
        session_minutes=60,
        train_where="gym",
    )
    database.apply_workout(
        {
            "id": "w1",
            "title": "Push",
            "description": None,
            "start_time": "2026-10-27T18:00:00Z",
            "end_time": "2026-10-27T19:00:00Z",
            "updated_at": "2026-10-27T19:00:00Z",
            "created_at": "2026-10-27T18:00:00Z",
            "exercises": [
                {
                    "index": 0,
                    "title": "Bench Press (Barbell)",
                    "notes": None,
                    "exercise_template_id": "05293BCA",
                    "superset_id": None,
                    "sets": [
                        {
                            "index": 0,
                            "type": "normal",
                            "weight_kg": 100,
                            "reps": 5,
                            "distance_meters": None,
                            "duration_seconds": None,
                            "rpe": None,
                            "custom_metric": None,
                        }
                    ],
                }
            ],
        },
        "2026-10-31T18:00:00Z",
    )


def test_incomplete_checklist_does_not_call_the_model(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="sk-test-key")
    model = CountingModel('{"summary":"nope","sessions":["nope"]}')

    def factory():
        model.calls += 100
        return model

    app.state.model_factory = factory
    response = client.post("/coach/routine", follow_redirects=True)
    assert response.status_code == 200
    assert "height" in response.text
    assert "Still needed before a routine" in response.text
    assert model.calls == 0
    assert "nope" not in response.text


def test_missing_height_is_not_guessed(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="sk-test-key")
    _seed_complete(app, height_cm=None)
    model = CountingModel('{"summary":"Use 72 inches.","sessions":["Upper"]}')
    app.state.model_factory = lambda: model
    response = client.post("/coach/routine", follow_redirects=True)
    assert "height" in response.text
    assert "72" not in response.text
    assert model.calls == 0


def test_complete_checklist_can_request_a_routine(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="sk-test-key")
    _seed_complete(app)
    model = CountingModel(
        '{"summary":"Keep the bench pattern you already logged.","sessions":["Upper: Bench Press"]}'
    )
    app.state.model_factory = lambda: model
    response = client.post("/coach/routine", follow_redirects=True)
    assert response.status_code == 200
    assert model.calls == 1
    assert "Keep the bench pattern you already logged." in response.text
    assert "Local routine proposal" in response.text
    assert "not in Hevy" in response.text


def test_invented_routine_weight_is_refused(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="sk-test-key")
    _seed_complete(app)
    model = CountingModel('{"summary":"Jump to 999 kg.","sessions":["Upper: Bench Press"]}')
    app.state.model_factory = lambda: model
    page = client.post("/coach/routine", follow_redirects=True)
    assert "999" not in page.text
    assert model.calls == 1


def test_research_failure_stops_paid_call(tmp_path, monkeypatch):
    from app.research import ResearchUnavailable
    client, app = signed_in(tmp_path, openai_api_key="test-value")
    _seed_complete(app)
    model = CountingModel('{}')
    app.state.model_factory = lambda: model
    def unavailable(*args):
        raise ResearchUnavailable()
    monkeypatch.setattr("app.routine.routine_evidence", unavailable)
    page = client.post("/coach/routine", follow_redirects=True)
    assert model.calls == 0
    assert "no AI call was made" in page.text


def test_gate_stops_research_too(tmp_path, monkeypatch):
    client, app = signed_in(tmp_path, openai_api_key="test-value")
    def should_not_fetch(*args):
        raise AssertionError("Incomplete profile must not trigger public research")
    monkeypatch.setattr("app.routine.routine_evidence", should_not_fetch)
    assert client.post("/coach/routine", follow_redirects=True).status_code == 200


def test_routine_has_persona_sources_and_respects_saved_days(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="test-value")
    _seed_complete(app)
    class Model(CountingModel):
        def invoke(self, messages):
            self.messages = messages
            return super().invoke(messages)
    model = Model('{"summary":"Big Dawg, keep the bench pattern.","sessions":["Upper: Bench Press"]}')
    app.state.model_factory = lambda: model
    page = client.post("/coach/routine", follow_redirects=True)
    assert model.calls == 1
    assert "Big Dawg" in model.messages[0].content
    assert "PUBLIC_RESEARCH_DATA" in model.messages[1].content
    assert "Research provided for this routine" in page.text
    assert 'https://pubmed.ncbi.nlm.nih.gov/41843416/' in page.text
    assert "2026-11-01T00:00:00+00:00" in page.text
    reused = client.post("/coach/routine", follow_redirects=True)
    assert model.calls == 1
    assert "No new AI call" in reused.text
    app.state.db.save_goal("Build strength with better recovery.", "2026-11-01T01:00:00Z")
    model.content = '{"summary":"Big Dawg, train.","sessions":["a","b","c","d","e"]}'
    page = client.post("/coach/routine", follow_redirects=True)
    assert "exceeded your saved training days" in page.text
