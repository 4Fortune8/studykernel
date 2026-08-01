"""FastAPI application. WEB_UI.md §3.

`Now`, the drill flow and profile switching. Three things about how this runs
matter more than any individual route:

1. **How the server starts.** `run()` hands `uvicorn.run` the application
   *object*, not the `"web.app:app"` import string. Uvicorn can only fork
   workers from an import string, so multiple workers are not merely
   discouraged here, they are unavailable. That matters because drill state is
   an in-process dict (`kernel.session.DrillStore`): under two workers a drill
   started on one is `UnknownDrill` on the other, roughly half the time, and
   the learner loses a blind capture they cannot honestly rewrite.

2. **It refuses to start pointed at nothing.** `STUDY_PRODUCT` and `STUDY_DB`
   are relative by default, so they resolve against the launch directory.
   Starting from the wrong one used to give a live server that 500ed on the
   home page *and* created an empty database wherever it stood -- a blank
   study history conjured by a `cd`. `deps.preflight()` now checks both before
   the first request and names the resolved paths.

3. **Losing a drill is a page, not a traceback.** A restart mid-drill is
   possible with one worker too, so `UnknownDrill` is handled and says what
   happened -- including that nothing was recorded.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kernel import config, session
from kernel.exchange import record as record_mod
from kernel.pedagogy import capture as capture_mod
from kernel.session import UnknownDrill
from kernel.storage import db
from web import deps, mathtext

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
# Presentation only -- see web/mathtext.py. Nothing stored is rewritten.
templates.env.filters["delimit_math"] = mathtext.delimit


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Run the migration once at boot rather than failing per request.

    Only `study init` called `migrate()` before, so a database created by an
    earlier version reaches the web layer missing columns and every page dies
    on a raw sqlite error. The migration is additive and idempotent -- it adds
    tables and columns and rewrites nothing -- so running it here costs
    nothing and removes a footgun the user cannot diagnose from a 500.
    """
    deps.preflight()
    conn = deps.connect()
    try:
        db.migrate(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="studykernel", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.exception_handler(config.PackError)
async def pack_broken(request: Request, exc: config.PackError) -> HTMLResponse:
    """A pack edited out from under a running server, rather than a traceback.

    `preflight()` catches the usual case at startup; this covers the pack
    changing while the server is up, which happens whenever a product is
    being edited in another window.
    """
    return templates.TemplateResponse(
        request=request,
        name="pack_error.html",
        context={"detail": str(exc), "cwd": Path.cwd()},
        status_code=500,
    )


@app.exception_handler(UnknownDrill)
async def drill_lost(request: Request, exc: UnknownDrill) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="drill_lost.html",
        context={"detail": str(exc)},
        status_code=410,
    )


# ------------------------------------------------------------------ profiles


@app.get("/profiles", response_class=HTMLResponse)
async def profiles(request: Request) -> HTMLResponse:
    conn = deps.connect()
    try:
        return templates.TemplateResponse(
            request=request,
            name="profiles.html",
            context={
                "profiles": db.list_profiles(conn),
                "active": deps.active_learner(request.cookies, conn),
            },
        )
    finally:
        conn.close()


@app.post("/profiles/switch")
async def switch_profile(learner_id: str = Form(...)) -> RedirectResponse:
    conn = deps.connect()
    try:
        if db.get_profile(conn, learner_id) is None:
            return RedirectResponse("/profiles", status_code=303)
    finally:
        conn.close()

    response = RedirectResponse("/", status_code=303)
    # No expiry: a profile choice on a machine you own should outlive the tab.
    # `samesite=lax` and no `secure` because this is served over plain http on
    # localhost by design.
    response.set_cookie(deps.PROFILE_COOKIE, learner_id, samesite="lax", max_age=31536000)
    return response


@app.post("/profiles/add")
async def add_profile(
    learner_id: str = Form(...), display_name: str = Form("")
) -> RedirectResponse:
    learner_id = learner_id.strip()
    if not learner_id:
        return RedirectResponse("/profiles", status_code=303)

    conn = deps.connect()
    try:
        db.ensure_learner(conn, learner_id, display_name.strip() or None)
    finally:
        conn.close()
    return await switch_profile(learner_id=learner_id)


# ---------------------------------------------------------------------- now


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """`Now`. WEB_UI.md §5.1: one question, three states.

    The satisfied state is a full-page stop with no way to start, per §6 --
    removed, not disabled. A study tool that cannot say *stop* is an
    engagement product, and this is the page where that gets decided.
    """
    conn = deps.connect()
    try:
        learner = deps.active_learner(request.cookies, conn)
        if learner is None:
            return RedirectResponse("/profiles", status_code=303)

        product = deps.load_product()
        drill = session.DrillSession(conn, product, learner)
        choice = drill.recommend()

        return templates.TemplateResponse(
            request=request,
            name="now.html",
            context={
                "profile": db.get_profile(conn, learner),
                "learner": learner,
                "product_dir": deps.product_dir(),
                "choice": choice,
                "satisfied": isinstance(choice, session.Satisfied),
                "starved": isinstance(choice, session.Starved),
                # Position is not shown on a satisfied page: the answer there
                # is "stop", and a progress readout invites one more session.
                "position": None if isinstance(choice, session.Satisfied) else drill.position(),
            },
        )
    finally:
        conn.close()


# -------------------------------------------------------------------- drill


def _session(request: Request, conn) -> session.DrillSession | None:
    learner = deps.active_learner(request.cookies, conn)
    if learner is None:
        return None
    return session.DrillSession(conn, deps.load_product(), learner)


def _panel(request: Request, drill: session.DrillSession, token: str, **extra):
    """Render the partial for whatever phase this drill is actually at.

    Phase comes from the server every time, so a refresh, a back button or a
    double-submit redraws the truth instead of replaying a step. The browser
    holds a token and nothing else.
    """
    view = drill.view(token)
    context = {"view": view, "served": view.served, "token": token, **extra}
    if view.phase is session.Phase.BRIEFED or view.phase is session.Phase.EXPLAINED:
        context.setdefault("briefing", drill.briefing(token))
    return templates.TemplateResponse(
        request=request, name=f"drill/_{view.phase.value}.html", context=context
    )


@app.post("/drill/start")
async def drill_start(request: Request, tag: str = Form("")) -> RedirectResponse:
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        served = drill.start(tag=tag or None)
        if not isinstance(served, session.Served):
            # Satisfied or starved -- `Now` is the page that says so properly.
            return RedirectResponse("/", status_code=303)
        return RedirectResponse(f"/drill/{served.token}", status_code=303)
    finally:
        conn.close()


@app.get("/drill/{token}", response_class=HTMLResponse)
async def drill_page(request: Request, token: str) -> HTMLResponse:
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        view = drill.view(token)
        briefing = (
            drill.briefing(token)
            if view.phase in (session.Phase.EXPLAINED, session.Phase.BRIEFED)
            else None
        )
        return templates.TemplateResponse(
            request=request,
            name="drill/page.html",
            context={
                "view": view,
                "served": view.served,
                "token": token,
                "briefing": briefing,
                "max_hint": session.hints.MAX_LEVEL,
            },
        )
    finally:
        conn.close()


@app.post("/drill/{token}/hint", response_class=HTMLResponse)
async def drill_hint(request: Request, token: str, level: int = Form(...)) -> HTMLResponse:
    """One rung, one request. WEB_UI.md §4.2.

    The ladder is never shipped whole and revealed client-side: the server
    hands over exactly the rung that was asked for and records that it did, so
    `min_hint_level` is a measurement rather than a self-report.
    """
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        rung = drill.request_hint(token, level)
        return templates.TemplateResponse(
            request=request,
            name="drill/_rung.html",
            context={"rung": rung, "token": token, "next_level": level + 1,
                     "max_hint": session.hints.MAX_LEVEL},
        )
    finally:
        conn.close()


@app.post("/drill/{token}/capture", response_class=HTMLResponse)
async def drill_capture(request: Request, token: str) -> HTMLResponse:
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        form = dict(await request.form())
        fields = drill.view(token).served.capture_fields
        try:
            drill.submit_capture(token, session.build_capture(form, fields))
        except capture_mod.CaptureError as exc:
            # Rejected captures do not advance the phase, so re-rendering the
            # panel puts the learner back on the same form with the reason.
            return _panel(request, drill, token, error=str(exc), submitted=form)
        return _panel(request, drill, token)
    finally:
        conn.close()


@app.post("/drill/{token}/answer", response_class=HTMLResponse)
async def drill_answer(request: Request, token: str, answer: str = Form("")) -> HTMLResponse:
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        drill.submit_answer(token, answer)
        return _panel(request, drill, token)
    finally:
        conn.close()


@app.post("/drill/{token}/explain", response_class=HTMLResponse)
async def drill_explain(
    request: Request, token: str, explanation: str = Form("")
) -> HTMLResponse:
    """The gate. There is no skip route here and there is not meant to be one."""
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        gate = drill.submit_explain_back(token, explanation)
        if not gate.passed:
            return _panel(request, drill, token, error=gate.reason, submitted={
                "explanation": explanation
            })
        return _panel(request, drill, token)
    finally:
        conn.close()


@app.post("/drill/{token}/record", response_class=HTMLResponse)
async def drill_record(request: Request, token: str, pasted: str = Form("")) -> HTMLResponse:
    """Inbound half of the exchange, validated inline. WEB_UI.md §4.5."""
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        briefing = drill.briefing(token)
        try:
            diagnosis = drill.record_for(token, pasted)
        except record_mod.RecordError as exc:
            return templates.TemplateResponse(
                request=request,
                name="drill/_exchange_result.html",
                context={"error": str(exc), "token": token, "briefing": briefing,
                         "pasted": pasted,
                         "record_url": f"/drill/{token}/record"},
            )
        return templates.TemplateResponse(
            request=request,
            name="drill/_exchange_result.html",
            context={"diagnosis": diagnosis, "token": token, "briefing": briefing},
        )
    finally:
        conn.close()


@app.post("/drill/{token}/waive")
async def drill_waive(request: Request, token: str) -> RedirectResponse:
    """"I don't need tutoring on this one." DESIGN.md §10.

    The exchange is the optional half: "no API is required for the system to
    work". Getting an item right and not wanting a tutor to explain it is the
    ordinary case, and making that a dead end -- or worse, a permanent nag in
    the report -- teaches people to stop finishing drills.

    Nothing is discarded. The attempt, the capture, the answer and the
    briefing all stay exactly where they were, and `/history` can pick the
    exchange back up whenever it is actually wanted.
    """
    conn = deps.connect()
    try:
        drill = _session(request, conn)
        if drill is None:
            return RedirectResponse("/profiles", status_code=303)
        attempt_id = drill.view(token).attempt_id
        if attempt_id is not None:
            db.waive_exchange(conn, attempt_id)
        drill.finish(token)
        return RedirectResponse("/", status_code=303)
    finally:
        conn.close()


# ------------------------------------------------------------------ history


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    tag: str = "",
    outcome: str = "",
    state: str = "",
) -> HTMLResponse:
    """Past questions answered, by category and outcome."""
    conn = deps.connect()
    try:
        learner = deps.active_learner(request.cookies, conn)
        if learner is None:
            return RedirectResponse("/profiles", status_code=303)
        product = deps.load_product()
        product_id = product["product_id"]
        return templates.TemplateResponse(
            request=request,
            name="history.html",
            context={
                "profile": db.get_profile(conn, learner),
                "attempts": db.list_past_attempts(
                    conn, learner, product_id,
                    tag_slug=tag or None, outcome=outcome or None, state=state or None,
                ),
                "tallies": db.tally_by_category(conn, learner, product_id),
                "tag": tag,
                "outcome": outcome,
                "state": state,
            },
        )
    finally:
        conn.close()


@app.get("/history/{attempt_id}", response_class=HTMLResponse)
async def history_item(request: Request, attempt_id: int) -> HTMLResponse:
    """One answered item, with the stored briefing if the exchange is still open."""
    conn = deps.connect()
    try:
        learner = deps.active_learner(request.cookies, conn)
        if learner is None:
            return RedirectResponse("/profiles", status_code=303)
        product = deps.load_product()
        past = db.get_past_attempt(conn, learner, product["product_id"], attempt_id)
        if past is None:
            return templates.TemplateResponse(
                request=request, name="not_found.html",
                context={"what": f"attempt {attempt_id}"}, status_code=404,
            )
        return templates.TemplateResponse(
            request=request,
            name="history_item.html",
            context={
                "profile": db.get_profile(conn, learner),
                "past": past,
                "briefing": db.load_briefing(conn, attempt_id),
            },
        )
    finally:
        conn.close()


@app.post("/history/{attempt_id}/record", response_class=HTMLResponse)
async def history_record(
    request: Request, attempt_id: int, pasted: str = Form("")
) -> HTMLResponse:
    """Finish an exchange that was skipped earlier, or never started."""
    conn = deps.connect()
    try:
        learner = deps.active_learner(request.cookies, conn)
        if learner is None:
            return RedirectResponse("/profiles", status_code=303)
        drill = session.DrillSession(conn, deps.load_product(), learner)
        try:
            diagnosis = drill.record(attempt_id, pasted)
        except record_mod.RecordError as exc:
            return templates.TemplateResponse(
                request=request, name="drill/_exchange_result.html",
                context={"error": str(exc), "attempt_id": attempt_id, "pasted": pasted,
                         "record_url": f"/history/{attempt_id}/record"},
            )
        # Recording supersedes a waiver: it is no longer skipped, it is done.
        db.waive_exchange(conn, attempt_id, waived=False)
        return templates.TemplateResponse(
            request=request, name="drill/_exchange_result.html",
            context={"diagnosis": diagnosis, "attempt_id": attempt_id},
        )
    finally:
        conn.close()


def run() -> None:
    """Entry point for the `study-web` console script.

    Passing `app` rather than "web.app:app" is what makes single-worker
    operation structural -- see this module's docstring.
    """
    import uvicorn

    deps.product_dir()  # fail loudly at startup, not on the first request
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
