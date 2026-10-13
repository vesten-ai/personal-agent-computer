"""The gateway's web server: the chat page, a small JSON API and one websocket per open agent.

  PAC_ADMIN_TOKEN=... python -m pac_gateway.server /etc/pac/gateway.toml

Sign in once with the admin token; it is then kept in an HttpOnly cookie.
"""

import asyncio
import contextlib
import hmac
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import config
from .agent import Runtime

STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "pac_token"


def build_app(cfg: config.Config) -> Starlette:
    state: dict = {}

    def authorized(conn) -> bool:
        want = cfg.admin_token
        if not want:
            return False
        got = conn.cookies.get(COOKIE) or conn.headers.get("authorization", "").removeprefix("Bearer ")
        return bool(got) and hmac.compare_digest(got, want)

    def rt() -> Runtime:
        return state["runtime"]

    def view(s) -> dict:
        r = s.row
        return {"id": r["id"], "name": r["name"], "model": r["model"], "status": r["status"], "busy": s.busy}

    async def index(request: Request):
        if not authorized(request):
            return FileResponse(os.path.join(STATIC, "login.html"), status_code=401)
        return FileResponse(os.path.join(STATIC, "index.html"))

    async def login(request: Request):
        form = await request.form()
        token = str(form.get("token", ""))
        resp = RedirectResponse("/", status_code=303)
        if token and cfg.admin_token and hmac.compare_digest(token, cfg.admin_token):
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400,
                            secure=request.url.scheme == "https")
        return resp

    async def list_agents(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse({"agents": [view(s) for s in rt().sessions.values()],
                             "models": list(cfg.models), "default_model": cfg.default_model})

    async def create_agent(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        body = await request.json()
        name = str(body.get("name", "")).strip()[:40]
        if not name:
            return JSONResponse({"error": "name required"}, 400)
        try:
            s = rt().create_agent(name, body.get("model") or None)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)
        return JSONResponse(view(s), 201)

    async def delete_agent(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        aid = request.path_params["aid"]
        if aid not in rt().sessions:
            return JSONResponse({"error": "not found"}, 404)
        await asyncio.to_thread(rt().delete_agent, aid)
        return JSONResponse({"ok": True})

    async def events(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        aid = request.path_params["aid"]
        after = int(request.query_params.get("after", 0))
        return JSONResponse({"events": rt().store.events(aid, after)})

    async def screen(request: Request):
        if not authorized(request):
            return Response(status_code=401)
        path = rt().store.screen_path(request.path_params["aid"])
        if not os.path.exists(path):
            return Response(status_code=404)
        return FileResponse(path, media_type="image/png", headers={"cache-control": "no-store"})

    async def agent_ws(ws: WebSocket):
        if not authorized(ws):
            await ws.close(code=4401)
            return
        session = rt().sessions.get(ws.path_params["aid"])
        if session is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        session.subscribers.add(q)

        async def pump():
            while True:
                await ws.send_json(await q.get())

        sender = asyncio.create_task(pump())
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "message" and str(msg.get("text", "")).strip():
                    await session.send(str(msg["text"]).strip())
                elif msg.get("type") == "stop":
                    session.interrupt()
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            session.subscribers.discard(q)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        state["runtime"] = Runtime(cfg, asyncio.get_running_loop())
        yield

    return Starlette(
        routes=[
            Route("/", index),
            Route("/login", login, methods=["POST"]),
            Route("/api/agents", list_agents, methods=["GET"]),
            Route("/api/agents", create_agent, methods=["POST"]),
            Route("/api/agents/{aid}", delete_agent, methods=["DELETE"]),
            Route("/api/agents/{aid}/events", events),
            Route("/api/agents/{aid}/screen.png", screen),
            WebSocketRoute("/api/agents/{aid}/ws", agent_ws),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    cfg = config.load(sys.argv[1] if len(sys.argv) > 1 else "/etc/pac/gateway.toml")
    if not cfg.admin_token:
        sys.exit(f"${cfg.admin_token_env} is not set")
    uvicorn.run(build_app(cfg), host=cfg.listen_host, port=cfg.listen_port, log_level="info")


if __name__ == "__main__":
    main()
