"""Phone-friendly companion for saved Hevy workouts, goals, check-ins, and coaching."""

from __future__ import annotations

import logging
import difflib
import secrets
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pymongo.errors import PyMongoError

from app.coach import CONTEXT_SENT, MISSING_KEY_MESSAGE, ask, page_facts
from app.config import Settings
from app.garmin_data import sync_garmin, garmin_page
from app.db import Database, open_database
from app.dashboard import dashboard_data, home_brief
from app.hevy.client import HevyClient, HevyError
from app.hevy.library import routine_library
from app.hevy.sync import run_sync
from app.hevy.webhook import (
    MAX_BODY_BYTES,
    WebhookPayloadError,
    WebhookWorker,
    authorization_matches,
    parse_workout_notice,
    process_webhook_jobs,
    webhook_status_text,
)
from app.macros import ACTIVITY_FACTORS, ensure_primary_goal, macro_guide
from app.metrics import information_gaps, summarize
from app.present import (
    CONFIRMED_LABEL,
    FEELINGS,
    GOAL_NOTICE,
    HEVY_RECORD_NOTICE,
    HISTORY_NOTICE,
    RECORD_NOTE,
    USER_REPORT_LABEL,
    USER_REPORT_NOTICE,
    VOLUME_NOTE,
    format_set,
    format_weight,
    prepare_exercises,
    volume_line,
)
from app.periods import history_period
from app.redact import redact
from app.routine import build_routine, routine_gaps
from app.training_plan import WeeklyPlan, save_plan_proposal
from app.hevy.routine_publish import publication_summary, RoutineAPI, PublicationBlocked
from app.plan_suggestions import load_catalog, record_suggestion, suggestion_state, decide_suggestion, SuggestionBlocked
from app.scheduling import SyncScheduler
from app.timeutil import to_iso

logger = logging.getLogger("fitness_coach")
ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
PAGE_SIZE = 20


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = open_database(settings)
    database.migrate()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        garmin_scheduler = None
        worker = None
        if settings.sync_enabled and settings.hevy_configured and settings.auth_configured:
            scheduler = SyncScheduler(
                trigger=lambda: _scheduled_sync(app),
                interval_seconds=max(300, settings.sync_interval_seconds),
            )
            scheduler.start()
            app.state.scheduler = scheduler
        if settings.garmin_sync_enabled and settings.garmin_configured and settings.auth_configured:
            garmin_scheduler = SyncScheduler(trigger=lambda: _garmin_sync(app),
                interval_seconds=max(3600, settings.garmin_sync_interval_seconds), initial_delay=40,
                name="garmin-sync")
            garmin_scheduler.start()
        if settings.webhook_worker_enabled and settings.webhook_configured and settings.hevy_configured:
            database.recover_running_webhook_jobs(to_iso(datetime.now(timezone.utc)))
            worker = WebhookWorker(lambda: _process_webhooks(app))
            worker.start()
            app.state.webhook_worker = worker
        yield
        if scheduler:
            scheduler.stop()
        if garmin_scheduler:
            garmin_scheduler.stop()
        if app.state.garmin_client:
            app.state.garmin_client.close()
        if worker:
            worker.stop()
        close = getattr(database, "close", None)
        if close:
            close()

    app = FastAPI(title="AmoreGymBro", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.sync_lock = threading.Lock()
    app.state.client_factory = None
    app.state.model_factory = None
    app.state.garmin_lock = threading.Lock()
    app.state.garmin_client_factory = None
    app.state.garmin_client = None
    app.state.plan_lock = threading.Lock()
    app.state.routine_api_factory = None
    app.state.hevy_reviews = {}

    @app.exception_handler(PyMongoError)
    async def database_unavailable(request: Request, exc):
        # Keep driver details/credentials out of the page and operational logs.
        logger.warning("Private database connection unavailable")
        return _render(request, "unavailable.html", {}, status_code=503)

    if settings.auth_configured:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.app_secret,
            session_cookie="coach_session",
            max_age=60 * 60 * 24 * 30,
            same_site="lax",
            https_only=settings.secure_cookies,
        )

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        database.count_active()
        return {"status": "ok"}

    @app.get("/robots.txt", response_class=HTMLResponse)
    def robots():
        return HTMLResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse(
            {
                "name": "AMORE GYM BRO",
                "short_name": "Zeus",
                "display": "standalone",
                "start_url": "/",
                "background_color": "#f5f5f8",
                "theme_color": "#f5f5f8",
                "icons": [
                    {"src": "/static/icon-180.png", "sizes": "180x180", "type": "image/png"}
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request):
        if settings.auth_configured:
            return RedirectResponse("/", status_code=303)
        return _render(request, "setup.html", {})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if not settings.auth_configured:
            return RedirectResponse("/setup", status_code=303)
        if request.session.get("signed_in") is True:
            return RedirectResponse("/", status_code=303)
        return _render(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, password: str = Form("")):
        if not settings.auth_configured:
            return RedirectResponse("/setup", status_code=303)
        if _passwords_match(password, settings.app_password):
            request.session["signed_in"] = True
            return RedirectResponse("/", status_code=303)
        return _render(
            request,
            "login.html",
            {"error": "That password did not match."},
            status_code=401,
        )

    @app.post("/logout")
    def logout(request: Request):
        if settings.auth_configured:
            request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, offset: int = 0):
        blocked = _require_user(request)
        if blocked:
            return blocked
        offset = max(0, offset)
        state = database.get_sync_state()
        workouts = database.list_workouts(PAGE_SIZE, offset)
        webhook_counts = database.webhook_counts()
        profile = database.get_profile() or {}
        dashboard = dashboard_data(database, profile.get("weight_unit"))
        return _render(
            request,
            "home.html",
            {
                "notice": HEVY_RECORD_NOTICE,
                "workouts": workouts,
                "state": state,
                "profile": profile,
                "dashboard": dashboard,
                "home_brief": home_brief(database, settings.app_timezone),
                "runs": database.recent_runs(),
                "hevy_configured": settings.hevy_configured,
                "garmin_configured": settings.garmin_configured,
                "garmin": garmin_page(database),
                "csrf_token": _csrf_token(request),
                "webhook_notice": webhook_status_text(settings.webhook_configured, webhook_counts),
                "webhook_failed": webhook_counts["failed"],
                "flash": request.session.pop("flash", None),
                "offset": offset,
                "page_size": PAGE_SIZE,
                "has_newer": offset > 0,
                "has_older": offset + PAGE_SIZE < dashboard["totals"]["confirmed_workout_count"],
            },
        )

    @app.get("/hevy/routines", response_class=HTMLResponse)
    def hevy_routines(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        rows, error, checked_at = [], None, None
        if settings.hevy_configured:
            try:
                with _hevy_client(app) as client:
                    profile = database.get_profile() or {}
                    rows = routine_library(
                        client,
                        profile.get("weight_unit"),
                        profile.get("distance_unit"),
                    )
                checked_at = to_iso(datetime.now(timezone.utc))
            except (HevyError, ValueError, TypeError, AttributeError):
                error = "Couldn’t load the full routine library. Try refreshing; your Hevy routines are unchanged."
        else:
            error = "Hevy is not connected on this instance."
        return _render(request, "hevy_routines.html", {"routines": rows,
            "error": error, "checked_at": checked_at}, status_code=502 if error and settings.hevy_configured else 200)

    @app.get("/workouts/{workout_id}", response_class=HTMLResponse)
    def workout_detail(request: Request, workout_id: str):
        blocked = _require_user(request)
        if blocked:
            return blocked
        workout = database.get_workout(workout_id)
        if workout is None:
            return _render(request, "missing.html", {}, status_code=404)
        profile = database.get_profile() or {}
        weight_unit = profile.get("weight_unit")
        distance_unit = profile.get("distance_unit")
        for exercise in workout["exercises"]:
            for entry in exercise["sets"]:
                entry["line"] = format_set(entry, weight_unit, distance_unit)
        return _render(
            request,
            "workout.html",
            {
                "workout": workout,
                "confirmed": CONFIRMED_LABEL,
                "notice": HEVY_RECORD_NOTICE,
            },
        )

    @app.get("/goals", response_class=HTMLResponse)
    def goals(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        ensure_primary_goal(database, _now())
        measurement = database.latest_body_measurement()
        entry = database.get_body_entry() or {}
        profile = database.get_profile() or {}
        weight_kg = None if measurement is None else measurement.get("weight_kg")
        guide = macro_guide(
            weight_kg=weight_kg,
            height_cm=entry.get("height_cm"),
            age_years=entry.get("age_years"),
            sex=entry.get("sex"),
            activity=entry.get("activity"),
        )
        return _render(
            request,
            "goals.html",
            {
                "notice": GOAL_NOTICE,
                "goals": database.list_goals(),
                "flash": request.session.pop("flash", None),
                "confirmed": CONFIRMED_LABEL,
                "report": USER_REPORT_LABEL,
                "measurement": measurement,
                "measurement_lines": _measurement_lines(measurement, profile.get("weight_unit")),
                "entry": entry,
                "guide": guide,
                "weight_line": format_weight(weight_kg, profile.get("weight_unit")),
                "activities": list(ACTIVITY_FACTORS),
            },
        )

    @app.post("/goals/body")
    def save_body_entry(
        request: Request,
        height_in: str = Form(""),
        age_years: str = Form(""),
        sex: str = Form(""),
        activity: str = Form(""),
        days_per_week: str = Form(""),
        session_minutes: str = Form(""),
        train_where: str = Form(""),
    ):
        blocked = _require_user(request)
        if blocked:
            return blocked
        height_cm = _optional_height_cm(height_in)
        age = _optional_age(age_years)
        chosen_sex = sex if sex in {"female", "male"} else None
        chosen_activity = activity if activity in ACTIVITY_FACTORS else None
        days = _optional_span(days_per_week, 1, 7)
        minutes = _optional_span(session_minutes, 20, 180)
        place = train_where if train_where in {"gym", "home"} else None
        if height_in.strip() and height_cm is None:
            request.session["flash"] = "Height needs to be a number of inches."
        elif age_years.strip() and age is None:
            request.session["flash"] = "Age needs to be a whole number."
        elif days_per_week.strip() and days is None:
            request.session["flash"] = "Days per week needs to be from 1 to 7."
        elif session_minutes.strip() and minutes is None:
            request.session["flash"] = "Session length needs to be from 20 to 180 minutes."
        else:
            database.save_body_entry(
                height_cm=height_cm,
                age_years=age,
                sex=chosen_sex,
                activity=chosen_activity,
                updated_at=_now(),
                days_per_week=days,
                session_minutes=minutes,
                train_where=place,
            )
            request.session["flash"] = "Body details saved."
        return RedirectResponse("/goals", status_code=303)

    @app.post("/goals")
    def save_goal(request: Request, statement: str = Form("")):
        blocked = _require_user(request)
        if blocked:
            return blocked
        text = statement.strip()
        if not text:
            request.session["flash"] = "Write a goal first."
        elif len(text) > 400:
            request.session["flash"] = "Keep the goal under 400 characters."
        else:
            database.save_goal(text, _now())
            request.session["flash"] = "Goal saved."
        return RedirectResponse("/goals", status_code=303)

    @app.post("/goals/{goal_id}/delete")
    def delete_goal(request: Request, goal_id: int):
        blocked = _require_user(request)
        if blocked:
            return blocked
        if database.delete_goal(goal_id):
            request.session["flash"] = "Goal removed."
        else:
            request.session["flash"] = "That goal is not saved."
        return RedirectResponse("/goals", status_code=303)

    @app.get("/check-ins", response_class=HTMLResponse)
    def check_ins(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        return _render(
            request,
            "checkins.html",
            {
                "notice": USER_REPORT_NOTICE,
                "label": USER_REPORT_LABEL,
                "feelings": FEELINGS,
                "check_ins": database.list_check_ins(),
                "flash": request.session.pop("flash", None),
            },
        )

    @app.post("/check-ins")
    def save_check_in(request: Request, feeling: str = Form(""), notes: str = Form("")):
        blocked = _require_user(request)
        if blocked:
            return blocked
        text = notes.strip()
        if feeling not in FEELINGS:
            request.session["flash"] = "Choose how you feel."
        elif len(text) > 1000:
            request.session["flash"] = "Keep the note under 1000 characters."
        else:
            database.save_check_in(feeling, text or None, _now())
            request.session["flash"] = "Check-in saved."
        return RedirectResponse("/check-ins", status_code=303)

    @app.post("/check-ins/{check_in_id}/delete")
    def delete_check_in(request: Request, check_in_id: int):
        blocked = _require_user(request)
        if blocked:
            return blocked
        if database.delete_check_in(check_in_id):
            request.session["flash"] = "Check-in removed."
        else:
            request.session["flash"] = "That check-in is not saved."
        return RedirectResponse("/check-ins", status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, period: str = "all"):
        blocked = _require_user(request)
        if blocked:
            return blocked
        selected = history_period(period, timezone_name=settings.app_timezone)
        exercises, totals, unit = _history_bundle(database, selected["start"], selected["end"])
        return _render(
            request,
            "history.html",
            {
                "notice": HISTORY_NOTICE,
                "confirmed": CONFIRMED_LABEL,
                "volume_note": VOLUME_NOTE,
                "record_note": RECORD_NOTE,
                "exercises": exercises,
                "totals": totals,
                "volume_line": volume_line(totals["total_volume_kg"], unit),
                "gaps": _history_gaps(database, totals, selected["key"]),
                "period": selected,
                "flash": request.session.pop("flash", None),
            },
        )

    @app.get("/history/exercise", response_class=HTMLResponse)
    def history_detail(request: Request, key: str = "", period: str = "all"):
        blocked = _require_user(request)
        if blocked:
            return blocked
        selected = history_period(period, timezone_name=settings.app_timezone)
        exercises, _, _ = _history_bundle(database, selected["start"], selected["end"])
        exercise = next((item for item in exercises if item["key"] == key), None)
        if exercise is None and selected["key"] != "all":
            all_exercises, _, _ = _history_bundle(database)
            known = next((item for item in all_exercises if item["key"] == key), None)
            if known:
                exercise = {
                    **known, "sessions": [], "session_count": 0, "working_set_count": 0,
                    "sets_missing_load": 0, "personal_record": None, "total_volume_kg": None,
                    "record_line": "No working set saved in this period.",
                    "volume_line": "Not calculated",
                }
        if exercise is None:
            return _render(
                request,
                "missing.html",
                {"heading": "That exercise is not in the selected period"},
                status_code=404,
            )
        return _render(
            request,
            "history_detail.html",
            {
                "notice": HISTORY_NOTICE,
                "confirmed": CONFIRMED_LABEL,
                "volume_note": VOLUME_NOTE,
                "record_note": RECORD_NOTE,
                "exercise": exercise,
                "period": selected,
            },
        )

    @app.get("/coach", response_class=HTMLResponse)
    def coach(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        return _render(request, "coach.html", _coach_context(request, database, settings))

    @app.post("/coach")
    def ask_coach(request: Request, question: str = Form("")):
        blocked = _require_user(request)
        if blocked:
            return blocked
        text = question.strip()
        if not text:
            request.session["flash"] = "Write a question first."
            return RedirectResponse("/coach", status_code=303)
        if len(text) > 1000:
            request.session["flash"] = "Keep the question under 1000 characters."
            return RedirectResponse("/coach", status_code=303)
        outcome = ask(
            database,
            settings,
            text,
            model_factory=app.state.model_factory,
        )
        if outcome["status"] == "missing_key":
            request.session["flash"] = outcome["message"]
            return RedirectResponse("/coach", status_code=303)
        proposal = outcome.pop("prepared_plan_suggestion", None)
        with app.state.plan_lock:
            if proposal:
                current = database.latest_training_plan()
                if current and current["plan_fingerprint"] == proposal["baseline_fingerprint"]:
                    record_suggestion(database, proposal, _now())
                    outcome["plan_suggestion"] = {key: proposal[key] for key in ("suggestion_id", "changes")}
                else:
                    outcome["proposal_notice"] = "Your plan changed while Zeus answered. Ask again to review the current plan."
            database.save_coach_reply(_now(), outcome["status"], text, outcome)
        return RedirectResponse("/coach", status_code=303)

    @app.post("/coach/plan/{suggestion_id}")
    def approve_chat_plan(request: Request, suggestion_id: str, decision: str = Form(""), csrf_token: str = Form("")):
        blocked = _require_user(request)
        if blocked:
            return blocked
        if not _csrf_valid(request, csrf_token):
            return HTMLResponse("Refresh the page and try again.", status_code=403)
        with app.state.plan_lock:
            try:
                latest_reply = database.latest_coach_reply()
                decide_suggestion(database, suggestion_id, decision, _now())
                if decision == "apply" and latest_reply and latest_reply.get("plan_suggestion", {}).get("suggestion_id") == suggestion_id:
                    database.save_coach_reply(_now(), "answered", latest_reply["question"], latest_reply)
                request.session["flash"] = ("Your weekly plan is updated in this app. Review it before saving changes to Hevy."
                                            if decision == "apply" else "Current plan kept. Nothing changed in Hevy.")
            except SuggestionBlocked as exc:
                request.session["flash"] = str(exc)
        return RedirectResponse("/coach#zeus-chat", status_code=303)

    @app.post("/coach/routine")
    def coach_routine(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        outcome = build_routine(database, settings, model_factory=app.state.model_factory)
        if outcome["status"] == "incomplete":
            request.session["flash"] = "Still needed before a routine: " + ", ".join(outcome["missing"]) + "."
            return RedirectResponse("/coach", status_code=303)
        if outcome["status"] == "missing_key":
            request.session["flash"] = outcome["message"]
            return RedirectResponse("/coach", status_code=303)
        database.save_coach_reply(_now(), outcome["status"], "Build my routine", outcome)
        return RedirectResponse("/coach", status_code=303)

    @app.get("/plan", response_class=HTMLResponse)
    def training_plan(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        saved = database.latest_training_plan()
        if saved is None:
            return _render(request, "missing.html", {"heading": "No weekly plan saved yet"}, status_code=404)
        plan = WeeklyPlan.model_validate(saved["weekly_plan"]).model_dump(mode="json")
        unit = (database.get_profile() or {}).get("weight_unit")
        for day in plan["days"]:
            for exercise in day["exercises"]:
                exercise["load_display"] = format_weight(exercise["weight_kg"], unit) if exercise["weight_kg"] is not None else None
        return _render(request, "plan.html", {"plan": plan, "saved": saved, "publication": publication_summary(database, saved), "flash": request.session.pop("flash", None)})

    @app.get("/plan/hevy", response_class=HTMLResponse)
    def review_hevy_changes(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        from app.hevy.routine_update import prepare_update_review
        review, error, token = None, None, None
        if not settings.hevy_configured and app.state.routine_api_factory is None:
            error = "The Hevy connection is not configured. Your routines are unchanged."
        else:
            api = app.state.routine_api_factory() if app.state.routine_api_factory else RoutineAPI(settings.hevy_api_key)
            try:
                review = prepare_update_review(database, api, load_catalog(settings), (database.get_profile() or {}).get("weight_unit"))
                for change in review["changes"]:
                    change["removed_lines"], change["added_lines"] = [], []
                    for operation, a, b, c, d in difflib.SequenceMatcher(a=change["before"], b=change["after"], autojunk=False).get_opcodes():
                        if operation in {"replace", "delete"}:
                            change["removed_lines"].extend(change["before"][a:b])
                        if operation in {"replace", "insert"}:
                            change["added_lines"].extend(change["after"][c:d])
                if review.get("can_approve"):
                    token = secrets.token_urlsafe(24)
                    now = datetime.now(timezone.utc)
                    with app.state.plan_lock:
                        app.state.hevy_reviews = {k: v for k, v in app.state.hevy_reviews.items() if v["expires"] > now}
                        old = request.session.pop("hevy_review_token", None)
                        app.state.hevy_reviews.pop(old, None)
                        # Bound abandoned previews without keeping payloads in browser cookies.
                        while len(app.state.hevy_reviews) >= 20:
                            app.state.hevy_reviews.pop(next(iter(app.state.hevy_reviews)))
                        app.state.hevy_reviews[token] = {"review": review, "expires": now + timedelta(minutes=10)}
                        request.session["hevy_review_token"] = token
            except PublicationBlocked as exc:
                error = str(exc)
            finally:
                api.close()
        return _render(request, "hevy_review.html", {"review": review, "error": error, "approval_token": token, "csrf_token": _csrf_token(request)})

    @app.post("/plan/hevy/approve")
    def approve_hevy_changes(request: Request, approval_token: str = Form(""), csrf_token: str = Form("")):
        blocked = _require_user(request)
        if blocked:
            return blocked
        if not _csrf_valid(request, csrf_token):
            return HTMLResponse("Refresh the review and try again.", status_code=403)
        from app.hevy.routine_update import apply_update_review
        with app.state.plan_lock:
            expected = request.session.pop("hevy_review_token", "")
            if not approval_token or not secrets.compare_digest(expected, approval_token):
                return HTMLResponse("Open a fresh Hevy review before saving.", status_code=409)
            stored = app.state.hevy_reviews.pop(approval_token, None)
            if not stored or stored["expires"] <= datetime.now(timezone.utc):
                return HTMLResponse("This review expired. Review the changes again.", status_code=409)
            api = app.state.routine_api_factory() if app.state.routine_api_factory else RoutineAPI(settings.hevy_api_key)
            try:
                result = apply_update_review(database, api, stored["review"],
                    approved_fingerprint=stored["review"]["plan_fingerprint"],
                    approval_note="Human approved the displayed before/after Hevy changes using Approve & save to Hevy.")
                request.session["flash"] = "Saved and verified in Hevy. Existing daily routines were reused."
            except PublicationBlocked as exc:
                request.session["flash"] = str(exc)
            finally:
                api.close()
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/draft")
    def save_training_plan(request: Request, plan: WeeklyPlan):
        blocked = _require_user(request)
        if blocked:
            return blocked
        with app.state.plan_lock:
            saved = save_plan_proposal(database, plan, _now())
        return {"status": "draft", "version": saved["plan_version"], "url": "/plan", "hevy_written": False}

    @app.get("/garmin", response_class=HTMLResponse)
    def garmin_details(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        return _render(request, "garmin.html", {
            "garmin": garmin_page(database),
            "garmin_configured": settings.garmin_configured,
            "garmin_auto_sync": settings.garmin_sync_enabled,
            "garmin_interval_hours": max(3600, settings.garmin_sync_interval_seconds) / 3600,
            "csrf_token": _csrf_token(request),
            "flash": request.session.pop("flash", None),
        })

    @app.post("/garmin/sync")
    def garmin_sync_now(request: Request, csrf_token: str = Form(""), return_to: str = Form("home")):
        blocked = _require_user(request)
        if blocked:
            return blocked
        if not _csrf_valid(request, csrf_token):
            return HTMLResponse("Refresh the page and try again.", status_code=403)
        if not settings.garmin_configured:
            request.session["flash"] = "Garmin key needed. Save it locally in the project’s 1Password item."
        else:
            outcome = _garmin_sync(app)
            request.session["flash"] = {"success": "Garmin daily summaries synced.",
                "busy": "Garmin sync is already running.",
                "failed": "Garmin sync did not finish. Previously saved summaries are preserved."}[outcome["status"]]
        return RedirectResponse("/garmin" if return_to == "garmin" else "/#garmin", status_code=303)

    @app.post("/sync")
    def sync_now(request: Request):
        blocked = _require_user(request)
        if blocked:
            return blocked
        if not settings.hevy_configured:
            request.session["flash"] = (
                "Save HEVY_API_KEY in the AmoreGymBro 1Password item, "
                "then restart it. Do not paste the key into chat."
            )
            return RedirectResponse("/", status_code=303)
        if not app.state.sync_lock.acquire(blocking=False):
            request.session["flash"] = "A sync is already running."
            return RedirectResponse("/", status_code=303)
        try:
            result = _sync(app)
        finally:
            app.state.sync_lock.release()
        if result["status"] == "success":
            message = (
                f"Sync finished. {result['upserted']} saved or updated, "
                f"{result['deleted']} deleted."
            )
            if result.get("warning"):
                message = f"{message} {result['warning']}"
        else:
            message = result.get("error") or "Sync failed."
        request.session["flash"] = message
        return RedirectResponse("/", status_code=303)

    @app.post("/webhooks/hevy")
    async def hevy_webhook(request: Request):
        """Acknowledge a saved-workout notice before any Hevy or model call."""
        if not settings.webhook_configured:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        provided = request.headers.get("authorization")
        if not authorization_matches(provided, settings.hevy_webhook_authorization):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        try:
            body = await _read_bounded(request, MAX_BODY_BYTES)
            workout_id = parse_workout_notice(body)
        except WebhookPayloadError:
            return JSONResponse({"detail": "Bad request"}, status_code=400)
        outcome = database.enqueue_webhook_job(workout_id, to_iso(datetime.now(timezone.utc)))
        return JSONResponse({"ok": True, "duplicate": outcome == "duplicate"})

    return app


async def _read_bounded(request: Request, limit: int) -> bytes:
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise WebhookPayloadError("payload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _require_user(request: Request):
    settings = request.app.state.settings
    if not settings.auth_configured:
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("signed_in") is not True:
        return RedirectResponse("/login", status_code=303)
    return None


def _render(request: Request, name: str, context: dict, status_code: int = 200):
    payload = {"request": request, **context}
    return templates.TemplateResponse(request, name, payload, status_code=status_code)


def _now() -> str:
    return to_iso(datetime.now(timezone.utc))


def _optional_height_cm(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        inches = float(text)
    except ValueError:
        return None
    if inches < 48 or inches > 90:
        return None
    return round(inches * 2.54, 1)


def _optional_span(raw: str, low: int, high: int) -> int | None:
    text = raw.strip()
    if not text or not text.isdigit():
        return None
    value = int(text)
    if value < low or value > high:
        return None
    return value


def _optional_age(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    if not text.isdigit():
        return None
    age = int(text)
    if age < 13 or age > 100:
        return None
    return age


def _measurement_lines(measurement: dict | None, weight_unit: str | None) -> list[tuple[str, str]]:
    if not measurement:
        return []
    labels = {
        "weight_kg": "Weight",
        "lean_mass_kg": "Lean mass",
        "fat_percent": "Body fat",
        "neck_cm": "Neck",
        "shoulder_cm": "Shoulders",
        "chest_cm": "Chest",
        "left_bicep_cm": "Left bicep",
        "right_bicep_cm": "Right bicep",
        "left_forearm_cm": "Left forearm",
        "right_forearm_cm": "Right forearm",
        "abdomen": "Abdomen",
        "waist": "Waist",
        "hips": "Hips",
        "left_thigh": "Left thigh",
        "right_thigh": "Right thigh",
        "left_calf": "Left calf",
        "right_calf": "Right calf",
    }
    lines = []
    for key, label in labels.items():
        value = measurement.get(key)
        if value is None:
            continue
        if key == "weight_kg" or key == "lean_mass_kg":
            shown = format_weight(value, weight_unit)
        elif key == "fat_percent":
            shown = f"{value:g}%"
        elif key.endswith("_cm"):
            shown = f"{value:g} cm"
        else:
            shown = f"{value:g}"
        lines.append((label, shown))
    return lines


def _history_bundle(database: Database, start: str | None = None, end: str | None = None):
    profile = database.get_profile() or {}
    summary = summarize(database.confirmed_exercise_rows(start, end), database.count_active(start, end))
    exercises = prepare_exercises(
        summary["exercises"],
        profile.get("weight_unit"),
        profile.get("distance_unit"),
    )
    return exercises, summary["totals"], profile.get("weight_unit")


def _history_gaps(database: Database, totals: dict, period: str = "all") -> list[str]:
    sync = database.get_sync_state()
    gaps = information_gaps(
        totals,
        goal_count=len(database.list_goals()),
        check_in_count=len(database.list_check_ins()),
        last_success_at=sync.get("last_success_at"),
        include_profile=False,
    )
    if period != "all":
        gaps = ["No confirmed workouts were saved in this period." if gap == "No confirmed Hevy workouts are stored." else gap for gap in gaps]
    return gaps


def _coach_context(request: Request, database: Database, settings: Settings) -> dict:
    reply = database.latest_coach_reply()
    if reply and reply.get("plan_suggestion"):
        try:
            _, state = suggestion_state(database, reply["plan_suggestion"]["suggestion_id"])
        except SuggestionBlocked:
            state = "unavailable"
        reply["plan_suggestion"]["state"] = state
    training_plan = database.latest_training_plan()
    return {
        "facts": page_facts(database),
        "reply": reply,
        "csrf_token": _csrf_token(request),
        "context_sent": CONTEXT_SENT,
        "model_configured": settings.model_configured,
        "model_name": settings.resolved_openai_model,
        "missing_key": MISSING_KEY_MESSAGE,
        "confirmed": CONFIRMED_LABEL,
        "flash": request.session.pop("flash", None),
        "routine_missing": routine_gaps(database),
        "training_plan": training_plan,
        "publication": publication_summary(database, training_plan),
    }


def _csrf_token(request):
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_urlsafe(24)
    return request.session["csrf_token"]


def _csrf_valid(request, provided):
    expected = request.session.get("csrf_token", "")
    return bool(provided and expected and secrets.compare_digest(expected, provided))


def _passwords_match(provided: str, expected: str) -> bool:
    provided_bytes = provided.encode()
    expected_bytes = expected.encode()
    if len(provided_bytes) != len(expected_bytes):
        return False
    return secrets.compare_digest(provided_bytes, expected_bytes)


def _garmin_sync(app):
    if not app.state.garmin_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        if app.state.garmin_client is None:
            from app.garmin.client import GarminClient
            factory = app.state.garmin_client_factory
            app.state.garmin_client = factory() if factory else GarminClient(app.state.settings.garmin_api_key)
        return sync_garmin(app.state.db, app.state.garmin_client, timezone_name=app.state.settings.app_timezone)
    finally:
        app.state.garmin_lock.release()


def _scheduled_sync(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    if not app.state.sync_lock.acquire(blocking=False):
        return
    try:
        _sync(app)
    except Exception as exc:
        logger.error("scheduled sync failed: %s", redact(str(exc), settings.redacted_secrets))
    finally:
        app.state.sync_lock.release()


def _process_webhooks(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    if not settings.hevy_configured or not settings.webhook_configured:
        return
    try:
        with _hevy_client(app) as client:
            process_webhook_jobs(
                app.state.db,
                client,
                now=datetime.now(timezone.utc),
                redact_secrets=settings.redacted_secrets,
                jitter_seconds=3,
            )
    except Exception as exc:
        logger.error("webhook worker failed: %s", redact(str(exc), settings.redacted_secrets))


@contextmanager
def _hevy_client(app: FastAPI):
    """Own a fresh read-only client per operation; close it even on failed reads."""
    settings: Settings = app.state.settings
    factory = app.state.client_factory
    client = factory() if factory else HevyClient(
        api_key=settings.hevy_api_key,
        base_url=settings.hevy_base_url,
        page_delay=settings.hevy_page_delay_seconds,
    )
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if close:
            close()


def _sync(app: FastAPI) -> dict:
    with _hevy_client(app) as client:
        return run_sync(
            app.state.db,
            client,
            redact_secrets=app.state.settings.redacted_secrets,
        )


app = create_app()
