from __future__ import annotations

import argparse
import base64
import json
import ssl
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def _base64url(value: int) -> str:
    width = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode()


class OidcServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], issuer: str, audience: str) -> None:
        super().__init__(address, OidcHandler)
        self.issuer = issuer
        self.audience = audience
        self.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_id = "guardrail-local-production-key"

    @property
    def jwks(self) -> dict[str, list[dict[str, str]]]:
        public = self.signing_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.key_id,
                    "n": _base64url(public.n),
                    "e": _base64url(public.e),
                }
            ]
        }


class OidcHandler(BaseHTTPRequestHandler):
    server: OidcServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/jwks.json":
            self._json(HTTPStatus.OK, self.server.jwks)
            return
        if path == "/.well-known/openid-configuration":
            self._json(
                HTTPStatus.OK,
                {
                    "issuer": self.server.issuer,
                    "jwks_uri": f"{self.server.issuer}/jwks.json",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/token":
            self._json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 16_384:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            subject = payload["subject"]
            roles = payload.get("roles", [])
            if not isinstance(subject, str) or not subject:
                raise ValueError("subject must be a non-empty string")
            if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
                raise ValueError("roles must be a string list")
            now = int(time.time())
            expires_in = int(payload.get("expires_in", 300))
            claims: dict[str, Any] = {
                "iss": payload.get("issuer", self.server.issuer),
                "aud": payload.get("audience", self.server.audience),
                "sub": subject,
                "email": f"{subject}@local.invalid",
                "roles": roles,
                "iat": now,
                "exp": now + expires_in,
            }
            token = jwt.encode(
                claims,
                self.server.signing_key,
                algorithm="RS256",
                headers={"kid": self.server.key_id},
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"detail": "Invalid token request"})
            return
        self._json(
            HTTPStatus.OK,
            {"access_token": token, "token_type": "Bearer", "expires_in": expires_in},
        )

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"oidc-mock {self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9443)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--certfile", required=True)
    parser.add_argument("--keyfile", required=True)
    arguments = parser.parse_args()

    server = OidcServer((arguments.host, arguments.port), arguments.issuer, arguments.audience)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(arguments.certfile, arguments.keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
