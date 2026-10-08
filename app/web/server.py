# server.py - Web 控制台（FastAPI）
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import ConfigManager
from ..database import monitored_video_manager, reply_record_manager, video_discovery_manager
from ..log_buffer import log_buffer
from ..logger import logger
from .auth import (
    SESSION_TTL_SEC,
    check_login_rate,
    check_password,
    clear_login_fail,
    create_session,
    extract_token_from_headers,
    get_web_username,
    is_auth_enabled,
    record_login_fail,
    require_password_for_host,
    revoke_session,
    verify_session,
)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "brc_web_token"


class AppConfigUpdate(BaseModel):
    default_check_interval: Optional[int] = None
    log_level: Optional[str] = None
    uploader_uid: Optional[str] = None
    video_discovery_interval: Optional[int] = None
    default_ai_style: Optional[str] = None
    video_after: Optional[str] = None
    video_before: Optional[str] = None
    video_date_ranges: Optional[List[Any]] = None
    video_full_sync_on_start: Optional[bool] = None
    web_host: Optional[str] = None
    web_port: Optional[int] = None
    # 密码不通过此接口修改，避免误清空；请直接改 config.yaml


class CredentialUpdate(BaseModel):
    sessdata: Optional[str] = None
    bili_jct: Optional[str] = None
    buvid3: Optional[str] = None
    dedeuserid: Optional[str] = None
    ac_time_value: Optional[str] = None


class QwenUpdate(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


class BilibiliUpdate(BaseModel):
    credential: Optional[CredentialUpdate] = None


class ConfigUpdateRequest(BaseModel):
    app: Optional[AppConfigUpdate] = None
    bilibili: Optional[BilibiliUpdate] = None
    qwen: Optional[QwenUpdate] = None
    # AI 风格含提示词，体积大且少改；如需改请编辑 config.yaml
    # ai_reply_styles 不再接受 Web 写入，降低误改与信息面


class VideoCreateRequest(BaseModel):
    bvid: str = Field(..., min_length=3)
    title: Optional[str] = None
    template: Optional[str] = "@{username} 感谢你的评论！"
    interval: Optional[int] = 60
    use_ai: bool = True
    ai_style: Optional[str] = None
    enabled: bool = True


class VideoUpdateRequest(BaseModel):
    title: Optional[str] = None
    template: Optional[str] = None
    interval: Optional[int] = None
    use_ai: Optional[bool] = None
    ai_style: Optional[str] = None
    enabled: Optional[bool] = None


class LoginRequest(BaseModel):
    username: Optional[str] = None
    password: str = ""


def _get_service():
    from ..main import service
    return service


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _request_token(request: Request) -> Optional[str]:
    return extract_token_from_headers(
        request.headers.get("authorization"),
        request.cookies.get(COOKIE_NAME),
    )


def _is_public_path(path: str) -> bool:
    if path in ("/", "/api/login", "/api/auth/status", "/favicon.ico"):
        return True
    if path.startswith("/static/"):
        return True
    return False


def create_app() -> FastAPI:
    # 永久关闭公开 API 文档，避免暴露接口结构
    app = FastAPI(title="B站自动回复控制台", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # 不向跨站页面泄露
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if _is_public_path(path) or not is_auth_enabled():
            return await call_next(request)

        token = _request_token(request)
        if verify_session(token):
            return await call_next(request)

        return JSONResponse(
            {"detail": "未登录或会话已过期，请先登录"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        enabled = is_auth_enabled()
        token = _request_token(request)
        return {
            "auth_required": enabled,
            "authenticated": (not enabled) or verify_session(token),
            "username": get_web_username() if enabled else None,
        }

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request):
        if not is_auth_enabled():
            return {"ok": True, "auth_required": False}

        ip = _client_ip(request)
        if not check_login_rate(ip):
            raise HTTPException(429, "尝试次数过多，请稍后再试")

        expected_user = get_web_username()
        username = (body.username or expected_user).strip()
        if username != expected_user or not check_password(body.password):
            record_login_fail(ip)
            logger.warning(f"[Web] 登录失败 ip={ip}")
            raise HTTPException(401, "用户名或密码错误")

        clear_login_fail(ip)
        token = create_session()
        logger.info(f"[Web] 登录成功 ip={ip}")
        # 令牌只放 HttpOnly Cookie，不在 JSON 里回传，降低 XSS 窃取风险
        resp = JSONResponse({"ok": True, "auth_required": True, "username": username})
        resp.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=SESSION_TTL_SEC,
            path="/",
        )
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        revoke_session(_request_token(request))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/")
    async def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            raise HTTPException(404, "前端页面缺失")
        return FileResponse(index_file)

    @app.get("/api/status")
    async def get_status():
        svc = _get_service()
        app_cfg = ConfigManager().get_app_config()
        enabled = monitored_video_manager.count(enabled_only=True)
        total = monitored_video_manager.count(enabled_only=False)
        replies = reply_record_manager.get_total_count()
        uid = app_cfg.get("uploader_uid") or ""
        discovered = video_discovery_manager.get_discovered_count(str(uid)) if uid else 0
        return {
            "running": bool(svc.running),
            "monitored_enabled": enabled,
            "monitored_total": total,
            "active_tasks": len([t for t in svc.tasks if not t.done()]),
            "reply_total": replies,
            "uploader_uid": uid,
            "discovered_total": discovered,
            "date_filter": ConfigManager().describe_video_date_filter(),
            "default_ai_style": app_cfg.get("default_ai_style", ""),
            "check_interval": app_cfg.get("default_check_interval", 60),
            "discovery_interval": app_cfg.get("video_discovery_interval", 3600),
        }

    @app.get("/api/logs")
    async def get_logs(limit: int = 200, after_id: int = 0):
        return {"logs": log_buffer.recent(limit=limit, after_id=after_id)}

    @app.websocket("/api/logs/ws")
    async def logs_ws(websocket: WebSocket):
        # 仅接受 Cookie 会话，禁止把 token 放在 URL query（会进代理日志）
        if is_auth_enabled():
            token = websocket.cookies.get(COOKIE_NAME)
            if not verify_session(token):
                await websocket.close(code=4401)
                return

        await websocket.accept()
        history = log_buffer.recent(limit=300)
        await websocket.send_json({"type": "snapshot", "logs": history})
        queue = log_buffer.subscribe()
        try:
            while True:
                item = await queue.get()
                await websocket.send_json({"type": "log", "log": item})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            log_buffer.unsubscribe(queue)

    @app.get("/api/replies")
    async def get_replies(limit: int = 50, bvid: Optional[str] = None):
        return {"replies": reply_record_manager.list_recent(limit=limit, bvid=bvid)}

    @app.get("/api/videos")
    async def list_videos(enabled_only: bool = False):
        videos = monitored_video_manager.list_videos(enabled_only=enabled_only)
        for v in videos:
            v["reply_count"] = reply_record_manager.get_replied_count(v["bvid"])
        return {"videos": videos}

    @app.post("/api/videos")
    async def add_video(body: VideoCreateRequest):
        bvid = body.bvid.strip()
        if not bvid.upper().startswith("BV"):
            raise HTTPException(400, "请输入有效的 BV 号")
        app_cfg = ConfigManager().get_app_config()
        ai_style = body.ai_style or app_cfg.get("default_ai_style", "natural")
        interval = body.interval or app_cfg.get("default_check_interval", 60)
        inserted = monitored_video_manager.add_or_update(
            bvid=bvid,
            title=body.title,
            template=body.template or "@{username} 感谢你的评论！",
            interval=interval,
            use_ai=body.use_ai,
            ai_style=ai_style,
            enabled=body.enabled,
        )
        svc = _get_service()
        if body.enabled and svc.running and bvid not in svc._monitored_bvids:
            await svc._add_monitor_tasks([bvid])
        video = monitored_video_manager.get_video(bvid)
        if video:
            video["reply_count"] = reply_record_manager.get_replied_count(bvid)
        logger.info(f"[Web] {'新增' if inserted else '更新'}监控视频: {bvid}")
        return {"ok": True, "inserted": inserted, "video": video}

    @app.put("/api/videos/{bvid}")
    async def update_video(bvid: str, body: VideoUpdateRequest):
        existing = monitored_video_manager.get_video(bvid)
        if not existing:
            raise HTTPException(404, "视频不存在")
        data = body.model_dump(exclude_unset=True)
        monitored_video_manager.add_or_update(
            bvid=bvid,
            title=data.get("title", existing.get("title")),
            template=data.get("template", existing.get("template")),
            interval=data.get("interval", existing.get("interval")),
            use_ai=data.get("use_ai", existing.get("use_ai")),
            ai_style=data.get("ai_style", existing.get("ai_style")),
            enabled=data.get("enabled", existing.get("enabled")),
        )
        updated = monitored_video_manager.get_video(bvid)
        svc = _get_service()
        if updated and updated.get("enabled") and svc.running and bvid not in svc._monitored_bvids:
            await svc._add_monitor_tasks([bvid])
        if updated:
            updated["reply_count"] = reply_record_manager.get_replied_count(bvid)
        logger.info(f"[Web] 更新监控视频: {bvid}")
        return {"ok": True, "video": updated}

    @app.post("/api/videos/{bvid}/toggle")
    async def toggle_video(bvid: str):
        existing = monitored_video_manager.get_video(bvid)
        if not existing:
            raise HTTPException(404, "视频不存在")
        new_enabled = not existing.get("enabled", True)
        monitored_video_manager.set_enabled(bvid, new_enabled)
        svc = _get_service()
        if new_enabled and svc.running and bvid not in svc._monitored_bvids:
            await svc._add_monitor_tasks([bvid])
        video = monitored_video_manager.get_video(bvid)
        if video:
            video["reply_count"] = reply_record_manager.get_replied_count(bvid)
        logger.info(f"[Web] {'启用' if new_enabled else '停用'}监控: {bvid}")
        return {"ok": True, "video": video}

    @app.delete("/api/videos/{bvid}")
    async def delete_video(bvid: str):
        ok = monitored_video_manager.delete(bvid)
        if not ok:
            raise HTTPException(404, "视频不存在")
        logger.info(f"[Web] 删除监控视频: {bvid}")
        return {"ok": True}

    @app.get("/api/config")
    async def get_config():
        # 已脱敏：不含 Cookie / API Key / web_password 等任何明文或掩码片段
        return ConfigManager().get_public_config()

    @app.put("/api/config")
    async def update_config(body: ConfigUpdateRequest):
        payload: Dict[str, Any] = {}
        if body.app is not None:
            payload["app"] = body.app.model_dump(exclude_none=True)
        if body.qwen is not None:
            payload["qwen"] = body.qwen.model_dump(exclude_none=True)
        if body.bilibili is not None:
            payload["bilibili"] = body.bilibili.model_dump(exclude_none=True)
        try:
            public = ConfigManager().update_from_web(payload)
        except Exception as e:
            raise HTTPException(400, str(e)) from e

        # 凭证变更后让 CredentialManager 重新读取
        try:
            from ..api_clients import CredentialManager, QwenClient
            CredentialManager().reset()
            QwenClient().reset()
        except Exception:
            pass

        svc = _get_service()
        app_cfg = ConfigManager().get_app_config()
        if app_cfg.get("uploader_uid") is not None:
            svc.uploader_uid = app_cfg.get("uploader_uid")
        if app_cfg.get("video_discovery_interval"):
            svc.video_discovery_interval = int(app_cfg.get("video_discovery_interval"))

        logger.info("[Web] 业务配置已更新（敏感字段仅写入、不回传）")
        return {"ok": True, "config": public}

    @app.get("/api/styles")
    async def list_styles():
        # 只返回风格 id/名称，不回传完整提示词
        styles = ConfigManager().get_ai_styles_config()
        return {
            "styles": [
                {"id": k, "name": (v or {}).get("name", k)}
                for k, v in styles.items()
            ]
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


async def start_web_server(host: str = "127.0.0.1", port: int = 8787):
    """启动 Web 控制台。

    使用独立线程 + 独立事件循环运行 uvicorn，避免与 bilibili_api.sync
    共用事件循环时出现“进程在跑但 8787 未监听”的问题。
    """
    import threading
    import uvicorn

    if require_password_for_host(host) and not is_auth_enabled():
        logger.error(
            f"Web 监听地址为 {host}（非本机），但未配置 app.web_password。"
            f"为防止外人访问，已拒绝启动控制台。请在 config.yaml 设置 web_password 后重启。"
        )
        return

    ready = threading.Event()
    errors: list = []

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            log_buffer.bind_loop(loop)
            app = create_app()

            @app.on_event("startup")
            async def _on_startup():
                log_buffer.bind_loop(asyncio.get_running_loop())
                ready.set()

            config = uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            # 嵌入式运行，避免抢主进程信号
            server.install_signal_handlers = False
            loop.run_until_complete(server.serve())
        except Exception as e:
            errors.append(e)
            ready.set()
            logger.error(f"Web 控制台线程异常: {e}", exc_info=True)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=_run, name="web-console", daemon=True)
    thread.start()

    # 最多等 8 秒确认端口起来
    for _ in range(80):
        if ready.is_set():
            break
        await asyncio.sleep(0.1)

    if errors:
        raise RuntimeError(f"Web 控制台启动失败: {errors[0]}") from errors[0]

    if not ready.is_set():
        # 再探测一次端口，兼容 startup 钩子未触发的情况
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    "127.0.0.1" if host in ("0.0.0.0", "::") else host,
                    port,
                ),
                timeout=1.0,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception as e:
            raise RuntimeError(
                f"Web 控制台未能在 {host}:{port} 监听，请检查端口占用与依赖: {e}"
            ) from e

    if is_auth_enabled():
        logger.info(
            f"Web 控制台已监听（密码保护）: http://{host}:{port}/  "
            f"用户名: {get_web_username()}"
        )
    else:
        logger.info(
            f"Web 控制台已监听（未设密码，仅建议本机）: http://{host}:{port}/"
        )

    # 伴随主服务存活；线程为 daemon，主进程退出即结束
    while thread.is_alive():
        await asyncio.sleep(1.0)
