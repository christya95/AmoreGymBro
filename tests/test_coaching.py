from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import Field

from app.db import Database
from app.metrics import information_gaps, summarize
from app.present import CONFIRMED_LABEL, USER_REPORT_LABEL
from tests.test_app import KEY, PASSWORD, signed_in

MODEL_KEY = "sk-test-not-a-real-key"


def _row(**overrides):
    base = {
        "workout_id": "w1",
        "workout_title": "Sample session",
        "start_time": "2024-01-01T15:00:00Z",
        "exercise_index": 0,
        "exercise_title": "Bench Press (Barbell)",
        "exercise_notes": None,
        "exercise_template_id": "BENCH1",
        "set_index": 0,
        "set_type": "normal",
        "weight_kg": 100,
        "reps": 5,
        "distance_meters": None,
        "duration_seconds": None,
        "rpe": None,
        "custom_metric": None,
    }
    base.update(overrides)
    return base


def _sample_sets():
    return [
        _row(set_index=0, set_type="warmup", weight_kg=60, reps=10),
        _row(set_index=1, set_type="normal", weight_kg=100, reps=5),
        _row(set_index=2, set_type="normal", weight_kg=100, reps=5),
        _row(set_index=3, set_type="normal", weight_kg=None, reps=8),
    ]


def test_volume_excludes_warmups_and_missing_load():
    summary = summarize(_sample_sets(), workout_count=1)
    totals = summary["totals"]
    assert totals["total_volume_kg"] == 1000
    assert totals["warmup_set_count"] == 1
    assert totals["working_set_count"] == 3
    assert totals["sets_missing_load"] == 1
    assert totals["sets_with_volume"] == 2
    exercise = summary["exercises"][0]
    assert exercise["personal_record"]["weight_kg"] == 100
    assert exercise["personal_record"]["reps"] == 5
    assert exercise["total_volume_kg"] == 1000


def test_personal_record_is_the_heaviest_working_weight():
    rows = [
        _row(set_index=0, weight_kg=90, reps=12),
        _row(
            workout_id="w2",
            set_index=0,
            weight_kg=100,
            reps=5,
            start_time="2024-02-01T15:00:00Z",
        ),
    ]
    summary = summarize(rows, workout_count=2)
    record = summary["exercises"][0]["personal_record"]
    assert record["weight_kg"] == 100
    assert record["reps"] == 5
    assert record["workout_id"] == "w2"
    assert summary["totals"]["total_volume_kg"] == 1580


def test_warmup_only_does_not_invent_a_zero_volume():
    summary = summarize(
        [_row(set_type="warmup", weight_kg=20, reps=10)],
        workout_count=1,
    )
    assert summary["totals"]["total_volume_kg"] is None
    assert summary["totals"]["warmup_set_count"] == 1
    gaps = information_gaps(
        summary["totals"],
        goal_count=0,
        check_in_count=0,
        last_success_at=None,
        include_profile=False,
    )
    assert any("volume was not calculated" in gap for gap in gaps)
    assert "0" not in " ".join(gaps)


def test_goals_and_check_ins_persist_and_stay_distinct(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY)
    app.state.db.apply_workout(
        {
            "id": "w1",
            "title": "Sample session",
            "start_time": "2024-01-01T15:00:00Z",
            "end_time": "2024-01-01T16:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "created_at": "2024-01-01T15:00:00Z",
            "exercises": [
                {
                    "index": 0,
                    "title": "Bench Press (Barbell)",
                    "notes": "steady",
                    "exercise_template_id": "BENCH1",
                    "sets": [
                        {"index": 0, "type": "warmup", "weight_kg": 60, "reps": 10},
                        {"index": 1, "type": "normal", "weight_kg": 100, "reps": 5},
                        {"index": 2, "type": "normal", "weight_kg": 100, "reps": 5},
                        {"index": 3, "type": "normal", "weight_kg": None, "reps": 8},
                    ],
                }
            ],
        },
        "2026-10-31T18:31:00Z",
    )
    saved_goal = client.post(
        "/goals",
        data={"statement": "<script>alert(1)</script> Build a stronger squat"},
        follow_redirects=True,
    )
    assert saved_goal.status_code == 200
    assert "Goal saved." in saved_goal.text
    assert "<script>alert(1)</script>" not in saved_goal.text
    assert "&lt;script&gt;" in saved_goal.text
    assert "Build a stronger squat" in saved_goal.text
    assert CONFIRMED_LABEL not in saved_goal.text

    saved_report = client.post(
        "/check-ins",
        data={"feeling": "Tired", "notes": "Slept badly"},
        follow_redirects=True,
    )
    assert "Check-in saved." in saved_report.text
    assert USER_REPORT_LABEL in saved_report.text
    assert "Tired" in saved_report.text
    assert "Slept badly" in saved_report.text
    assert CONFIRMED_LABEL not in saved_report.text
    assert "Sample session" not in saved_report.text

    workout = client.get("/workouts/w1")
    assert CONFIRMED_LABEL in workout.text
    assert USER_REPORT_LABEL not in workout.text
    assert "Tired" not in workout.text
    assert "Slept badly" not in workout.text
    assert KEY not in workout.text

    again = Database(app.state.db.path)
    statements = [item["statement"] for item in again.list_goals()]
    assert any("stronger squat" in statement for statement in statements)
    assert again.list_check_ins()[0]["feeling"] == "Tired"
    assert again.list_check_ins()[0]["notes"] == "Slept badly"
    stored_titles = [item["title"] for item in again.list_workouts(20, 0)]
    assert stored_titles == ["Sample session"]

    history = client.get("/history")
    assert CONFIRMED_LABEL in history.text
    assert "1000 kg" in history.text
    assert "100 kg" in history.text
    assert "1600" not in history.text
    assert USER_REPORT_LABEL not in history.text
    detail = client.get("/history/exercise", params={"key": "id:BENCH1"})
    assert "warmup" in detail.text
    assert "1000 kg" in detail.text
    assert KEY not in detail.text


def test_empty_history_does_not_invent_zero_volume(tmp_path):
    client, _app = signed_in(tmp_path)
    page = client.get("/history")
    assert "No confirmed" in page.text
    assert "0 kg" not in page.text
    assert "Not calculated" in page.text


def test_anonymous_cannot_save_a_goal(tmp_path):
    client, app = signed_in(tmp_path)
    client.cookies.clear()
    response = client.post(
        "/goals",
        data={"statement": "Hidden goal"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert app.state.db.list_goals() == []


def test_coach_page_names_proffessor_zeus(tmp_path):
    client, _app = signed_in(tmp_path)
    page = client.get("/coach")
    assert "Professor Zeus" in page.text
    assert "Ask Professor Zeus" in page.text
    assert "Amore Gym Bro" in page.text


def test_history_packet_keeps_only_recent_sessions():
    from app.coach import HISTORY_SESSION_CAP, _history_payload

    sessions = []
    for index in range(20):
        sessions.append(
            {
                "workout_id": f"w{index}",
                "start_time": f"2024-01-{(index % 28) + 1:02d}T15:00:00Z",
                "session_volume_kg": 100,
                "sets_missing_load": 0,
                "untrusted_note": None,
                "sets": [
                    {
                        "set_type": "normal",
                        "weight_kg": 100,
                        "reps": 5,
                        "volume_kg": 500,
                        "distance_meters": None,
                        "duration_seconds": None,
                        "rpe": None,
                    }
                ],
            }
        )
    summary = {
        "exercises": [
            {
                "title": "Bench Press (Barbell)",
                "exercise_template_id": "BENCH1",
                "session_count": 20,
                "sessions": sessions,
                "total_volume_kg": 2000,
                "sets_missing_load": 0,
                "personal_record": {"weight_kg": 100, "reps": 5},
            }
        ]
    }
    payload = _history_payload(summary, "bench")
    match = payload["matches"][0]
    assert len(match["recent_sessions"]) == HISTORY_SESSION_CAP
    assert match["older_sessions_omitted"] == 20 - HISTORY_SESSION_CAP
    assert "distance_meters" not in match["recent_sessions"][0]["sets"][0]


def test_coach_without_a_model_key_does_not_call_the_model(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY, openai_api_key="")

    def boom():
        raise AssertionError("model was constructed")

    app.state.model_factory = boom
    page = client.get("/coach")
    assert "OPENAI_API_KEY" in page.text
    assert "1Password" in page.text
    assert "Do not paste the key into chat" in page.text
    assert KEY not in page.text
    assert PASSWORD not in page.text
    asked = client.post("/coach", data={"question": "How should I train?"}, follow_redirects=True)
    assert "OPENAI_API_KEY" in asked.text
    assert "1Password" in asked.text
    assert app.state.db.latest_coach_reply() is None


class RecordingModel(GenericFakeChatModel):
    seen: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _reply(action: str, workout_ids: list[str] | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "CoachReply",
                "args": {
                    "observed_facts": ["A confirmed workout is stored."],
                    "suggested_action": action,
                    "supporting_workout_ids": workout_ids or [],
                    "uncertainty": "I cannot see exercise form and cannot diagnose an injury.",
                    "missing_information": ["No check-in is saved."],
                },
                "id": "reply",
                "type": "tool_call",
            }
        ],
    )


def _script(action: str, workout_ids: list[str] | None = None) -> RecordingModel:
    return RecordingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "training_profile",
                            "args": {},
                            "id": "profile",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "exercise_history",
                            "args": {"exercise_query": "Bench"},
                            "id": "history",
                            "type": "tool_call",
                        }
                    ],
                ),
                _reply(action, workout_ids),
            ]
        )
    )


def _seed_bench(app):
    app.state.db.apply_workout(
        {
            "id": "w1",
            "title": "Sample session",
            "start_time": "2024-01-01T15:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "exercises": [
                {
                    "index": 0,
                    "title": "Bench Press (Barbell)",
                    "notes": KEY,
                    "exercise_template_id": "BENCH1",
                    "sets": [
                        {"index": 0, "type": "normal", "weight_kg": 100, "reps": 5},
                        {"index": 1, "type": "normal", "weight_kg": 100, "reps": 5},
                    ],
                }
            ],
        },
        "2026-10-31T18:31:00Z",
    )


def test_coach_refuses_an_invented_total(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY, openai_api_key=MODEL_KEY)
    _seed_bench(app)
    model = _script("Your weekly volume is 99999 kg.", ["w1"])
    app.state.model_factory = lambda: model
    page = client.post("/coach", data={"question": "How should I train?"}, follow_redirects=True)
    assert page.status_code == 200
    assert "was not calculated" in page.text
    assert "99999" not in page.text
    assert "1000 kg" in client.get("/history").text
    assert KEY not in page.text
    assert MODEL_KEY not in page.text
    assert PASSWORD not in page.text
    seen = str(model.seen)
    assert "training_profile" in seen or "1000" in seen
    assert KEY not in seen
    assert MODEL_KEY not in seen
    assert "[redacted]" in seen


def test_coach_shows_a_suggestion_that_uses_calculated_numbers(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY, openai_api_key=MODEL_KEY)
    _seed_bench(app)
    model = _script("The calculated volume is 1000 kg.", ["w1"])
    app.state.model_factory = lambda: model
    page = client.post("/coach", data={"question": "How should I train?"}, follow_redirects=True)
    assert "The calculated volume is 1000 kg." in page.text
    assert "Confirmed workout" in page.text
    assert KEY not in page.text
    assert MODEL_KEY not in page.text


def test_coach_refuses_to_judge_form(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY, openai_api_key=MODEL_KEY)
    _seed_bench(app)
    model = _script("Your form looks solid.")
    app.state.model_factory = lambda: model
    page = client.post("/coach", data={"question": "How should I train?"}, follow_redirects=True)
    assert "cannot see your exercise form" in page.text
    assert "looks solid" not in page.text
