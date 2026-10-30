from __future__ import annotations

import html
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .feishu_models import CALLBACK_PATH, FeishuConfigUpdate
from .services.feishu_notifications import FeishuError, FeishuNotifications

COOKIE = "uniform_feishu_binding"


class FeishuCallbackLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if isinstance(path, str) and path.split("?", 1)[0] == CALLBACK_PATH:
                record.args = (client, method, CALLBACK_PATH, version, status)
        return True


def register_feishu_routes(app: FastAPI, allowed_hosts: set[str]) -> None:
    router = APIRouter(prefix="/api/v1/system/feishu")
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, FeishuCallbackLogFilter) for item in logger.filters):
        logger.addFilter(FeishuCallbackLogFilter())

    def guard(request: Request, *, mutate: bool = False) -> FeishuNotifications:
        if request.headers.get("X-AI-Capability"):
            raise HTTPException(403, "飞书通知配置仅允许用户操作")
        if mutate:
            if request.headers.get("X-Uniform-Action") != "feishu-notifications":
                raise HTTPException(403, "缺少通知配置操作标识")
            if (request.url.hostname or "").casefold().rstrip(".") not in allowed_hosts:
                raise HTTPException(421, "通知配置访问地址不可信")
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "不允许跨站修改通知配置")
        return request.app.state.feishu_notifications

    def invoke(action: Any, *args: Any) -> Any:
        try:
            return action(*args)
        except FeishuError as error:
            raise HTTPException(409, str(error)) from error
        except (OSError, ValueError) as error:
            raise HTTPException(503, "通知配置存储或环境不可用") from error

    @router.get("")
    def status(request: Request) -> JSONResponse:
        return JSONResponse(invoke(guard(request).status), headers={"Cache-Control": "no-store"})

    @router.put("/config")
    async def configure(request: Request) -> JSONResponse:
        manager = guard(request, mutate=True)
        body = await request.body()
        if len(body) > 32_768:
            raise HTTPException(413, "配置内容过大")
        try:
            config = FeishuConfigUpdate.model_validate(json.loads(body))
        except (ValueError, ValidationError) as error:
            # Never echo input: validation errors can otherwise include App Secret.
            if isinstance(error, ValidationError):
                errors = error.errors(include_input=False, include_context=False, include_url=False)
                detail = "配置无效：" + "; ".join(item["msg"] for item in errors[:4])
            else:
                detail = "配置格式错误"
            raise HTTPException(422, detail) from error
        if (
            config.app_secret
            and request.url.scheme != "https"
            and request.url.hostname
            not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
        ):
            raise HTTPException(400, "请在本机地址或 HTTPS 页面配置应用密钥")
        result = await run_in_threadpool(invoke, manager.configure, config)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/bind")
    def bind(request: Request) -> JSONResponse:
        manager = guard(request, mutate=True)
        if request.url.scheme != "https" and request.url.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise HTTPException(400, "请在本机地址或 HTTPS 页面绑定飞书")
        redirect = urlsplit(manager.status()["config"]["redirect_uri"])
        if redirect.scheme != request.url.scheme or redirect.netloc != request.url.netloc:
            raise HTTPException(409, "请使用与回调地址相同的平台地址打开此页面，再进行扫码绑定")
        nonce = secrets.token_urlsafe(32)
        url = invoke(manager.begin_binding, nonce)
        response = JSONResponse(
            {"authorization_url": url, "expires_in": 300}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            COOKIE,
            nonce,
            max_age=300,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path=CALLBACK_PATH,
        )
        return response

    @router.delete("/binding")
    def unbind(request: Request) -> JSONResponse:
        manager = guard(request, mutate=True)
        invoke(manager.unbind)
        return JSONResponse(manager.status(), headers={"Cache-Control": "no-store"})

    @router.post("/test")
    def test(request: Request) -> JSONResponse:
        return JSONResponse(
            invoke(guard(request, mutate=True).test_message), headers={"Cache-Control": "no-store"}
        )

    @router.get("/events")
    def events(request: Request, limit: int = Query(default=100, ge=1, le=200)) -> JSONResponse:
        return JSONResponse(
            invoke(guard(request).events, limit), headers={"Cache-Control": "no-store"}
        )

    @app.get(CALLBACK_PATH, include_in_schema=False)
    def callback(request: Request) -> HTMLResponse:
        manager: FeishuNotifications = request.app.state.feishu_notifications
        diagnostic_html = ""
        try:
            redirect = urlsplit(manager.status()["config"]["redirect_uri"])
            if request.url.scheme != redirect.scheme or request.url.netloc != redirect.netloc:
                raise FeishuError("当前回调地址与配置不一致，请从原平台地址重新绑定")
            manager.complete_binding(
                request.query_params.get("state", ""),
                request.query_params.get("code", ""),
                request.cookies.get(COOKIE, ""),
                authorization_error=request.query_params.get("error", ""),
            )
            message = "飞书收件人绑定成功。请返回系统监控页，先发送测试消息，再开启自动告警。"
            code = 200
        except FeishuError as error:
            message, code = str(error), 400
            diagnostic = manager.status().get("last_binding")
            if diagnostic and diagnostic["attempt_id"] == error.binding_attempt_id:
                detail = f"诊断编号：{diagnostic['attempt_id']}；阶段：{diagnostic['stage']}"
                if diagnostic["error_code"] is not None:
                    detail += f"；飞书错误码：{diagnostic['error_code']}"
                diagnostic_html = "<p>" + html.escape(detail) + "</p>"
        except Exception:
            message, code = "绑定失败，请返回系统监控页重新操作。", 503
        response = HTMLResponse(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Uniform 飞书通知绑定</title><h2>Uniform 飞书通知绑定</h2><p>"
            + html.escape(message)
            + "</p>"
            + diagnostic_html
            + "<p>此页是 Uniform 平台回调页，不是飞书官方授权页。</p>"
            "<a href='/'>返回平台</a></html>",
            status_code=code,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                ),
            },
        )
        response.delete_cookie(COOKIE, path=CALLBACK_PATH)
        return response

    app.include_router(router)
