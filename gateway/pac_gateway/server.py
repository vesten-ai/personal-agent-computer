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
        status = "asleep" if s.sleep_state and not s.busy and r["status"] == "ready" else r["status"]
        return {"id": r["id"], "name": r["name"], "model": r["model"], "status": status, "busy": s.busy,
                "taken_over": s.taken_over}

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

    def session_for(request: Request):
        """(session, None) for an authorized request about a known agent, else (None, error response)."""
        if not authorized(request):
            return None, JSONResponse({"error": "unauthorized"}, 401)
        session = rt().sessions.get(request.path_params["aid"])
        if session is None:
            return None, JSONResponse({"error": "not found"}, 404)
        return session, None

    async def post_message(request: Request):
        session, err = session_for(request)
        if err:
            return err
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "text required"}, 400)
        return JSONResponse({"event_id": await session.send(text)}, 202)

    async def post_answer(request: Request):
        session, err = session_for(request)
        if err:
            return err
        body = await request.json()
        session.answer(str(body.get("id", "")), str(body.get("text", "")).strip()[:2000])
        return JSONResponse({"ok": True})

    async def post_interrupt(request: Request):
        session, err = session_for(request)
        if err:
            return err
        session.interrupt()
        return JSONResponse({"ok": True})

    async def post_wake(request: Request):
        session, err = session_for(request)
        if err:
            return err
        if session.busy:
            return JSONResponse({"error": "the agent is working, so its computer is awake"}, 409)
        await asyncio.to_thread(session.wake)
        return JSONResponse(view(session))

    async def post_sleep(request: Request):
        session, err = session_for(request)
        if err:
            return err
        how = (await request.json()).get("how", "paused")
        if how not in ("paused", "stopped"):
            return JSONResponse({"error": "how must be paused or stopped"}, 400)
        if session.busy or session.row["status"] != "ready":
            return JSONResponse({"error": f"the agent is {session.row['status']}; try again when it is ready"}, 409)
        await asyncio.to_thread(session.sleep, how, False)
        return JSONResponse(view(session))

    async def events(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        aid = request.path_params["aid"]
        if request.query_params.get("last"):
            return JSONResponse({"events": rt().store.last_events(aid, min(int(request.query_params["last"]), 500))})
        after = int(request.query_params.get("after") or 0)
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
                elif msg.get("type") == "take_over":
                    session.take_over()
                elif msg.get("type") == "hand_back":
                    session.hand_back()
                elif msg.get("type") == "answer":
                    session.answer(str(msg.get("id", "")), str(msg.get("text", "")).strip()[:2000])
                elif msg.get("type") == "wake" and not session.busy:
                    asyncio.create_task(asyncio.to_thread(session.wake))
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            session.subscribers.discard(q)

    async def vnc_ws(ws: WebSocket):
        """The agent's screen for noVNC: websocket bytes <-> x11vnc on the VM's loopback, through Incus exec."""
        if not authorized(ws):
            await ws.close(code=4401)
            return
        session = rt().sessions.get(ws.path_params["aid"])
        if session is None or session.row["status"] in ("creating", "error"):
            await ws.close(code=4404)
            return
        vm = session.row["vm"]
        if (await asyncio.to_thread(rt().manager.info, vm)).state != "Running":
            await ws.close(code=4409, reason="the computer is asleep")
            return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        session.viewers += 1
        session.touch()
        proc = await asyncio.create_subprocess_exec(
            *rt().manager.vnc_argv(vm), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)

        async def to_vm():
            while True:
                data = await ws.receive_bytes()
                proc.stdin.write(data)
                await proc.stdin.drain()

        async def to_browser():
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    return
                await ws.send_bytes(data)

        tasks = [asyncio.create_task(to_vm()), asyncio.create_task(to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            session.viewers -= 1
            session.touch()
            for t in tasks:
                t.cancel()
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            with contextlib.suppress(Exception):
                await ws.close()

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
            Route("/api/agents/{aid}/messages", post_message, methods=["POST"]),
            Route("/api/agents/{aid}/answer", post_answer, methods=["POST"]),
            Route("/api/agents/{aid}/interrupt", post_interrupt, methods=["POST"]),
            Route("/api/agents/{aid}/wake", post_wake, methods=["POST"]),
            Route("/api/agents/{aid}/sleep", post_sleep, methods=["POST"]),
            Route("/api/agents/{aid}/screen.png", screen),
            WebSocketRoute("/api/agents/{aid}/ws", agent_ws),
            WebSocketRoute("/api/agents/{aid}/vnc", vnc_ws),
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
