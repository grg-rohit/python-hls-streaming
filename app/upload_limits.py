"""Reject oversized/concurrent uploads before multipart spooling fills /tmp."""
import json
import os
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class UploadLimitsMiddleware:
    def __init__(self, app):
        self.app = app
        self.active = 0
        self.max_uploads = int(os.getenv("MAX_CONCURRENT_UPLOADS", "1"))
        self.max_file_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024**3)))
        if self.max_uploads < 1 or self.max_file_bytes < 1:
            raise ValueError("MAX_CONCURRENT_UPLOADS and MAX_UPLOAD_BYTES must be positive")
        # File size is checked exactly by the endpoint. Bound multipart overhead
        # too, including chunked requests and requests without Content-Length.
        self.max_body_bytes = self.max_file_bytes + 64 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/upload" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
            if length < 0:
                raise ValueError()
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
        if length > self.max_body_bytes:
            return await JSONResponse({"detail": "Upload exceeds configured limit"}, status_code=413)(scope, receive, send)
        if self.active >= self.max_uploads:
            return await JSONResponse({"detail": "Another upload is in progress; retry shortly"}, status_code=429,
                                      headers={"Retry-After": "5"})(scope, receive, send)
        self.active += 1  # No await between checking and incrementing on this event loop.
        total = 0
        exceeded = False
        sent_error_body = False
        error_body = json.dumps({"detail": "Upload exceeds configured limit"}).encode()

        async def limited_receive():
            nonlocal total, exceeded
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    exceeded = True
                    # Starlette closes partially spooled files on this exception.
                    raise MultiPartException("Upload exceeds configured limit")
            return message

        async def limited_send(message):
            nonlocal sent_error_body
            if exceeded:
                if message["type"] == "http.response.start":
                    message = {"type": "http.response.start", "status": 413, "headers": [
                        (b"content-type", b"application/json"), (b"content-length", str(len(error_body)).encode())]}
                elif message["type"] == "http.response.body":
                    if sent_error_body:
                        return
                    sent_error_body = True
                    message = {"type": "http.response.body", "body": error_body}
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        finally:
            self.active -= 1
