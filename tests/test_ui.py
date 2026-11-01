"""Display regressions: calendar dates, data provenance and usable forms."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.present import volume_line
from tests.test_app import settings, signed_in
from tests.test_routine import _seed_complete

ROOT = Path(__file__).resolve().parents[1]


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.detail_ancestors = []
        self.detail_stack = []
        self.form_depth = 0
        self.feed(html)
        assert self.form_depth == 0
        assert not self.detail_stack

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            assert self.form_depth == 0, "Nested forms break native submission"
            self.form_depth += 1
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        self.detail_ancestors.append(tuple(self.detail_stack))
        if tag == "details":
            self.detail_stack.append(attrs.get("id"))

    def handle_endtag(self, tag):
        if tag == "form":
            self.form_depth -= 1
        if tag == "details":
            self.detail_stack.pop()

    def by_id(self, name):
        return next(attrs for _, attrs in self.elements if attrs.get("id") == name)

    def panels_for(self, **attributes):
        return next(parents for (_, attrs), parents in zip(self.elements, self.detail_ancestors)
                    if all(attrs.get(key) == value for key, value in attributes.items()))


@pytest.mark.parametrize("zone", ["America/Toronto", "UTC", "Pacific/Auckland"])
def test_calendar_measurement_dates_do_not_shift_with_timezone(zone):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the display-script timezone check")
    script = r'''
const fs = require('fs');
const vm = require('vm');
const nodes = [
  {dateTime:'2026-10-21', textContent:'2026-10-21', hasAttribute:()=>false},
  {dateTime:'2026-10-21T23:30:00Z', textContent:'original', hasAttribute:()=>false},
  {dateTime:'not-a-date', textContent:'unchanged', hasAttribute:()=>false},
  {dateTime:'2026-10-21T23:30:00Z', textContent:'original', hasAttribute:()=>true}
];
vm.runInNewContext(fs.readFileSync('app/static/local-time.js','utf8'), {
  document:{querySelectorAll:()=>nodes}
});
console.log(JSON.stringify({
  rendered:nodes.map(node=>node.textContent),
  expected:[
    new Date(2026,9,21,12).toLocaleString(undefined,{dateStyle:'medium'}),
    new Date('2026-10-21T23:30:00Z').toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}),
    'unchanged',
    new Date('2026-10-21T23:30:00Z').toLocaleString(undefined,{dateStyle:'medium'})
  ]
}));
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, env={**os.environ, "TZ": zone}, text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)
    assert data["rendered"] == data["expected"]


def test_body_details_start_unset_and_macros_wait_for_inputs(tmp_path):
    client, _ = signed_in(tmp_path)
    response = client.get("/goals")
    page = Page(response.text)
    for field in ["height_in", "age_years", "days_per_week", "session_minutes"]:
        assert page.by_id(field)["value"] == ""
    for field in ["sex", "activity", "train_where"]:
        assert page.by_id(field)["name"] == field
    assert "Estimated daily guide" in response.text
    assert "<dt>Calories</dt>" not in response.text
    assert "Still needed" in response.text


def test_private_database_outage_has_actionable_page_not_driver_details(tmp_path, monkeypatch):
    from pymongo.errors import ServerSelectionTimeoutError
    client, app = signed_in(tmp_path)
    def unavailable():
        raise ServerSelectionTimeoutError("secret-driver-details")
    monkeypatch.setattr(app.state.db, "count_active", unavailable)
    response = client.get("/health")
    assert response.status_code == 503
    assert "temporarily unavailable" in response.text
    assert "secret-driver-details" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_saved_weight_date_and_macro_units_are_explicit(tmp_path):
    client, app = signed_in(tmp_path)
    _seed_complete(app)
    app.state.db.upsert_body_measurement({"date": "2026-10-25", "weight_kg": None}, "2026-10-31T18:00:00Z")
    home = client.get("/")
    assert 'Last saved weight · <time datetime="2026-10-21" data-date-only>' in home.text
    assert "Connected to Hevy" not in home.text
    # Remove the empty later measurement from this display scenario by adding a complete record.
    app.state.db.upsert_body_measurement({"date": "2026-10-26", "weight_kg": 84}, "2026-10-31T18:00:00Z")
    goals = client.get("/goals")
    assert "kcal/day" in goals.text and "g/day" in goals.text
    assert "not measured energy needs or food intake" in goals.text
    assert 'datetime="2026-10-26" data-date-only' in goals.text
    assert "Estimated daily maintenance (TDEE)" in goals.text


def test_route_pass_retains_forms_and_escapes_latest_reply(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="test-api-value")
    _seed_complete(app)
    unsafe = '<script>alert("unsafe")</script>'
    app.state.db.save_check_in("Tired", unsafe, "2026-10-31T18:00:00Z")
    app.state.db.save_coach_reply("2026-10-31T18:00:00Z", "answered", unsafe, {
        "message": unsafe, "suggested_action": unsafe, "observed_facts": [unsafe],
        "missing_information": [unsafe], "uncertainty": unsafe, "supporting_workout_ids": ["w1"],
    })
    for route in ["/", "/history", "/history/exercise?key=id:05293BCA", "/goals", "/check-ins", "/coach", "/workouts/w1"]:
        response = client.get(route)
        assert response.status_code == 200, route
        Page(response.text)
        assert unsafe not in response.text
        assert 'aria-label="Sections"' in response.text
        assert 'class="brand-logo"' in response.text
    coach = client.get("/coach").text
    assert "&lt;script&gt;" in coach
    assert coach.index('id="data-disclosure"') < coach.index("Ask Proffessor Zeus")
    assert "ChatGPT subscription does not cover it" in coach
    assert "Configured model:" in coach
    assert '/workouts/w1' in coach
    field = Page(coach).by_id("question")
    assert field["maxlength"] == "1000" and "required" in field
    assert 'method="post" action="/coach/routine"' in coach
    checkins = Page(client.get("/check-ins").text)
    assert any(tag == "option" and attrs.get("value") == "" and "disabled" in attrs for tag, attrs in checkins.elements)
    assert client.get("/workouts/unknown").status_code == 404
    assert client.get("/history/exercise?key=unknown").status_code == 404


def test_login_and_setup_keep_brand_and_never_request_secret_entry(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    login = client.get("/login")
    page = Page(login.text)
    assert page.by_id("password")["autocomplete"] == "current-password"
    assert 'class="brand-logo"' in login.text
    assert '<span>AMORE</span><span>GYM BRO</span>' in login.text
    assert 'aria-label="Sections"' not in login.text
    setup_client = TestClient(create_app(settings(tmp_path, app_password="", app_secret="")))
    setup = setup_client.get("/setup")
    Page(setup.text)
    assert "1Password" in setup.text
    assert "./start-local.sh" in setup.text
    assert ".env.app.references" in setup.text and "references only" in setup.text
    assert "APP_SECRET" in setup.text and "HEVY_API_KEY" in setup.text
    assert "ordinary configuration" in setup.text
    assert "<input" not in setup.text


def test_missing_activity_guides_user_to_field_and_keeps_saved_profile_visible(tmp_path):
    client, app = signed_in(tmp_path)
    _seed_complete(app)
    app.state.db.save_body_entry(
        height_cm=177.8, age_years=35, sex="male", activity=None,
        days_per_week=4, session_minutes=60, train_where="gym",
        updated_at="2026-11-01T12:00:00Z",
    )

    def no_model_call():
        raise AssertionError("Profile and macros must not call a model")

    app.state.model_factory = no_model_call
    response = client.get("/goals")
    assert response.context["guide"]["missing"] == ["activity"]
    html = response.text
    page = Page(html)
    assert 'href="#saved-profile"' in html and 'href="#daily-macros"' in html
    assert 'href="#activity">Choose activity level</a>' in html
    assert "Waiting for activity level" in html
    assert html.count('class="daily-macro-tile macro-pending"') == 4
    assert html.count('aria-label="Not calculated">—') == 4
    assert "<dt>Calories</dt>" not in html
    assert "open" in page.by_id("body-details")
    assert page.by_id("activity")["aria-describedby"] == "activity-help"
    assert "Training days alone do not determine activity level" in html
    snapshot = html.split('id="saved-profile"', 1)[1].split("</section>", 1)[0]
    assert "<input" not in snapshot and "<select" not in snapshot
    for value in ["177.8 cm", "70.0 in", "35 years", "Male", "4 per week", "60 minutes", "Gym", "Not set", "2026-11-01T12:00:00Z"]:
        assert value in snapshot
    assert html.index('id="saved-profile"') < html.index('id="body-details"')

    saved = client.post("/goals/body", data={
        "height_in": "70", "age_years": "35", "sex": "male", "activity": "moderate",
        "days_per_week": "4", "session_minutes": "60", "train_where": "gym",
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert saved.context["guide"]["complete"] is True
    assert "Waiting for activity level" not in saved.text
    assert 'class="daily-macro-tile macro-pending"' not in saved.text
    assert "open" not in Page(saved.text).by_id("body-details")
    snapshot = saved.text.split('id="saved-profile"', 1)[1].split("</section>", 1)[0]
    assert "Moderate · regular movement and exercise" in snapshot
    for field, unit in [("calories", "kcal/day"), ("protein_g", "g/day"), ("carb_g", "g/day"), ("fat_g", "g/day")]:
        value = saved.context["guide"][field]
        assert f"<strong>{value}</strong><span>{unit}</span>" in saved.text
    assert 'value="moderate" selected' in saved.text
    assert 'method="post" action="/goals/body"' in saved.text
    assert "no AI charge" in saved.text


def test_empty_saved_profile_shows_unset_fields_without_fabricated_defaults(tmp_path):
    client, _ = signed_in(tmp_path)
    html = client.get("/goals").text
    snapshot = html.split('id="saved-profile"', 1)[1].split("</section>", 1)[0]
    assert "No profile saved yet" in snapshot
    assert snapshot.count("Not set") == 7
    assert "0 years" not in snapshot and "0 minutes" not in snapshot
    assert 'href="/#sync-details">Sync saved Hevy weight</a>' in html


def test_zeus_avoids_duplicate_navigation_and_keeps_disclosure(tmp_path):
    client, app = signed_in(tmp_path)
    app.state.model_factory = lambda: (_ for _ in ()).throw(AssertionError("Viewing chat must not call a model"))
    html = client.get("/coach").text
    assert 'aria-label="Sections"' in html
    assert "Your saved data" not in html
    assert 'id="workout-data"' not in html
    assert 'id="data-disclosure"' in html
    assert 'method="post" action="/coach/routine"' in html


def test_zeus_welcome_is_static_and_plan_outline_does_not_claim_updates(tmp_path):
    client, _ = signed_in(tmp_path)
    html = client.get("/coach").text
    assert "Big Dawg" in html and "💪" in html
    assert "No question yet. Start with the box below." in html
    assert "Your latest question and answer" not in html
    assert "does not automatically edit your saved weekly plan" in html


@pytest.mark.parametrize("published", [False, True])
def test_zeus_collapsible_cards_keep_composer_and_plan_status(tmp_path, monkeypatch, published):
    from tests.test_training_plan import example_plan

    client, app = signed_in(tmp_path, openai_api_key="test-api-value")
    app.state.model_factory = lambda: pytest.fail("Viewing cards must not call a model")
    app.state.client_factory = lambda: pytest.fail("Viewing cards must not call Hevy")
    client.post("/plan/draft", json=example_plan())
    monkeypatch.setattr("app.main.publication_summary", lambda *_: {
        "complete": published, "routines": [{}] if published else [], "folder": "AMORE GYM BRO",
    })
    html = client.get("/coach").text
    page = Page(html)
    for panel_id in ["zeus-chat", "weekly-plan"]:
        assert ("details", page.by_id(panel_id)) in page.elements
    assert "open" in page.by_id("zeus-chat")
    assert "open" not in page.by_id("weekly-plan")
    assert page.panels_for(id="question") == ("zeus-chat",)
    assert page.panels_for(action="/coach") == ("zeus-chat",)
    assert page.panels_for(id="data-disclosure") == ("zeus-chat",)
    assert html.index('id="data-disclosure"') < html.index("Ask Proffessor Zeus")
    assert page.panels_for(href="/plan") == ("weekly-plan",)
    summary = html.split('id="weekly-plan"', 1)[1].split("</summary>", 1)[0]
    assert ("saved to Hevy" if published else "draft") in summary
    assert "Test weekly plan" in summary
    assert "Draft a weekly outline" not in html
    assert "You choose whether to update your plan" in html
    assert "/static/profile-details.js" in html

    plan_html = client.get("/plan").text
    plan_page = Page(plan_html)
    link = next(attrs for tag, attrs in plan_page.elements
                if tag == "a" and attrs.get("href") == "/coach#question")
    assert "button" in link["class"].split()
    assert "Ask Zeus for a swap" in plan_html
    assert "Update your plan only if you approve" in plan_html
    assert "proposed targets, not completed workouts" in plan_html
    assert ("Saved to Hevy" if published else "Proposed plan · not in Hevy") in plan_html
    assert ("daily routines are verified" in plan_html) is published


def test_question_anchor_reveals_chat_and_focuses_composer():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the disclosure-link check")
    script = r'''
const fs = require('fs');
const vm = require('vm');
const panel = {open:false};
let focused = false;
const target = {
  closest:selector => selector === 'details' ? panel : null,
  matches:selector => selector === 'input, select, textarea',
  focus:options => {focused = options.preventScroll;}
};
vm.runInNewContext(fs.readFileSync('app/static/profile-details.js','utf8'), {
  document:{getElementById:id => id === 'question' ? target : null, querySelectorAll:()=>[]},
  window:{location:{hash:'#question'}}
});
console.log(JSON.stringify({open:panel.open, focused}));
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {"open": True, "focused": True}
