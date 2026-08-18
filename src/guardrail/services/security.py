from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jwt
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from guardrail.config import RuntimeSettings
from guardrail.domain.auth import (
    ApplicationRole,
    AuthenticationError,
    Principal,
    TokenAuthenticator,
)

LOCAL_PRINCIPAL = Principal(
    subject="local-development",
    email=None,
    roles=frozenset(ApplicationRole),
)


class OidcAuthenticator:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        signing_key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        if not settings.oidc_issuer or not settings.oidc_audience or not settings.oidc_jwks_url:
            raise ValueError("OIDC issuer, audience, and JWKS URL are required.")
        # The issuer is an exact-match security boundary. Do not normalize it: an
        # identity provider may intentionally publish either form.
        self._issuer = settings.oidc_issuer
        self._audience = settings.oidc_audience
        self._roles_claim = settings.oidc_roles_claim
        if signing_key_resolver is None:
            client = PyJWKClient(
                settings.oidc_jwks_url,
                cache_keys=True,
                lifespan=300,
                timeout=5,
            )

            def resolve_signing_key(token: str) -> Any:
                return client.get_signing_key_from_jwt(token).key

            signing_key_resolver = resolve_signing_key
        self._signing_key_resolver = signing_key_resolver

    def authenticate(self, token: str) -> Principal:
        try:
            key = self._signing_key_resolver(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
            raw_roles = claims.get(self._roles_claim, [])
            if not isinstance(raw_roles, list) or not all(
                isinstance(role, str) for role in raw_roles
            ):
                raise AuthenticationError("OIDC roles claim is invalid.")
            roles = frozenset(_known_roles(raw_roles))
            return Principal(
                subject=claims["sub"],
                email=claims.get("email"),
                roles=roles,
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("Bearer token is invalid.") from exc


class AuthorizationService:
    def __init__(
        self,
        settings: RuntimeSettings,
        authenticator: TokenAuthenticator | None = None,
    ) -> None:
        self._enabled = settings.auth_enabled
        self._authenticator = authenticator
        self._bearer = HTTPBearer(auto_error=False)
        if self._enabled and self._authenticator is None:
            self._authenticator = OidcAuthenticator(settings)

    def require(self, role: ApplicationRole) -> Callable[[Request], Principal]:
        def dependency(
            request: Request,
            credentials: HTTPAuthorizationCredentials | None = Security(  # noqa: B008
                self._bearer
            ),
        ) -> Principal:
            principal = self._principal(
                request,
                credentials.credentials if credentials is not None else None,
            )
            if role not in principal.roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="The authenticated principal lacks the required role.",
                )
            return principal

        return dependency

    def any_authenticated(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(  # noqa: B008
            HTTPBearer(auto_error=False)  # noqa: B008
        ),
    ) -> Principal:
        return self._principal(
            request,
            credentials.credentials if credentials is not None else None,
        )

    def _principal(self, request: Request, token: str | None) -> Principal:
        if not self._enabled:
            request.state.principal = LOCAL_PRINCIPAL
            return LOCAL_PRINCIPAL
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A bearer token is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        assert self._authenticator is not None
        try:
            principal = self._authenticator.authenticate(token)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token is invalid.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        request.state.principal = principal
        return principal


def _known_roles(raw_roles: list[str]) -> tuple[ApplicationRole, ...]:
    roles: list[ApplicationRole] = []
    for raw_role in raw_roles:
        try:
            roles.append(ApplicationRole(raw_role))
        except ValueError:
            continue
    return tuple(roles)
