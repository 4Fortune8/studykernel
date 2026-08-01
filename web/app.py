"""FastAPI application. WEB_UI.md §3.

Phase 1 is not built yet: what exists here is the skeleton, the profile
switcher, and the two things that have to be right before any page is written.

1. **How the server starts.** `run()` hands `uvicorn.run` the application
   *object*, not the `"web.app:app"` import string. Uvicorn can only fork
   workers from an import string, so multiple workers are not merely
   discouraged here, they are unavailable. That matters because drill state is
   an in-process dict (`kernel.session.DrillStore`): under two workers a drill
   started on one is `UnknownDrill` on the other, roughly half the time, and
   the learner loses a blind capture they cannot honestly rewrite.

2. **Losing a drill is a page, not a traceback.** A restart mid-drill is
   possible with one worker too, so `UnknownDrill` is handled and says what
   happened -- including that nothing was recorded.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kernel import session
from kernel.session import UnknownDrill
from kernel.storage import db
from web import deps

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="studykernel", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


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


def run() -> None:
    """Entry point for the `study-web` console script.

    Passing `app` rather than "web.app:app" is what makes single-worker
    operation structural -- see this module's docstring.
    """
    import uvicorn

    deps.product_dir()  # fail loudly at startup, not on the first request
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
