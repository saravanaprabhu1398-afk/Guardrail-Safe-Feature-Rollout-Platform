from __future__ import annotations

from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyTooLargeError(RuntimeError):
    pass


class RequestSecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        production: bool = False,
    ) -> None:
        self._app = app
        self._max_request_bytes = max_request_bytes
        self._production = production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                exceeds_limit = int(content_length) > self._max_request_bytes
            except ValueError:
                exceeds_limit = True
            if exceeds_limit:
                response = JSONResponse(
                    {"detail": "Request body exceeds the configured limit."},
                    status_code=413,
                )
                await response(scope, receive, send)
                return

        request_id = headers.get(b"x-request-id", b"").decode(errors="ignore")[:128]
        request_id = request_id or str(uuid4())
        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_request_bytes:
                    raise RequestBodyTooLargeError
            return message

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if self._production:
                    response_headers["Strict-Transport-Security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
                    response_headers["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'"
                    )
            await send(message)

        try:
            await self._app(scope, limited_receive, secure_send)
        except RequestBodyTooLargeError:
            response = JSONResponse(
                {"detail": "Request body exceeds the configured limit."},
                status_code=413,
            )
            await response(scope, receive, secure_send)
