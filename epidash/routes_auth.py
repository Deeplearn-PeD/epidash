"""Viewer identity: Keycloak OIDC login + signed session cookies.

Enabled when ``EPIDASH_OIDC_*`` settings are present (see
``Settings.oidc_configured``). With identity off, all routes here still
exist but /auth/me reports an anonymous viewer and /auth/login returns
503 — anonymous viewing keeps working exactly as before.

Session cookies are HMAC-signed (viewer_id + expiry) using
``EPIDASH_SESSION_SECRET``; no server-side session state.
"""

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any, Optional

import httpx
import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from epidash.config import get_settings
from epidash.epidbot_client import epidbot_user_exists
from epidash.logging import get_logger
from epidash.store import Store

logger = get_logger(__name__)

router = APIRouter(include_in_schema=False)

_store: Store | None = None
# OIDC discovery cache (issuer -> document)
_discovery: dict[str, dict[str, Any]] = {}
_jwks_clients: dict[str, pyjwt.PyJWKClient] = {}

SESSION_COOKIE = "epidash_session"
SESSION_TTL_S = 30 * 24 * 3600


def set_store(store: Store) -> None:
    global _store
    _store = store


def get_store() -> Store:
    if _store is None:
        raise RuntimeError("Store not initialised; app startup did not run")
    return _store


# ── session cookie helpers ──────────────────────────────────────────────


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(viewer_id: int, secret: str) -> str:
    expires = int(time.time()) + SESSION_TTL_S
    payload = f"{viewer_id}.{expires}"
    return f"{payload}.{_sign(payload, secret)}"


def parse_session_cookie(value: str, secret: str) -> Optional[int]:
    """Return the viewer_id for a valid, unexpired cookie, else None."""
    try:
        viewer_id, expires, signature = value.split(".")
        payload = f"{viewer_id}.{expires}"
        if not hmac.compare_digest(signature, _sign(payload, secret)):
            return None
        if int(expires) < time.time():
            return None
        return int(viewer_id)
    except (ValueError, TypeError):
        return None


def get_viewer_from_request(request: Request) -> Optional[dict]:
    """Viewer row for the in-flight request, or None when anonymous."""
    settings = get_settings()
    if not settings.oidc_configured:
        return None
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    viewer_id = parse_session_cookie(cookie, settings.session_secret)
    if viewer_id is None:
        return None
    return get_store().get_viewer(viewer_id)


def set_session_cookie(response: RedirectResponse, viewer_id: int) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(viewer_id, settings.session_secret),
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https"),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ── OIDC plumbing ───────────────────────────────────────────────────────


async def _discover(issuer: str) -> dict[str, Any]:
    if issuer not in _discovery:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            )
            response.raise_for_status()
            _discovery[issuer] = response.json()
    return _discovery[issuer]


def _jwks_client_for(issuer: str, jwks_uri: str) -> pyjwt.PyJWKClient:
    if issuer not in _jwks_clients:
        _jwks_clients[issuer] = pyjwt.PyJWKClient(jwks_uri, cache_keys=True)
    return _jwks_clients[issuer]


def _state_secret() -> str:
    return get_settings().session_secret


async def exchange_code(code: str) -> dict[str, Any]:
    """Swap an authorization code for tokens and validate the id_token.

    Returns the validated id_token claims (sub, name, email, ...).
    """
    settings = get_settings()
    discovery = await _discover(settings.oidc_issuer)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "redirect_uri": settings.oidc_redirect,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Token exchange failed")
    id_token = response.json().get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="No id_token in token response")

    jwks_client = _jwks_client_for(settings.oidc_issuer, discovery["jwks_uri"])
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        claims = pyjwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_client_id,
            issuer=settings.oidc_issuer,
        )
    except pyjwt.PyJWTError as e:
        logger.warning("id_token validation failed: %s", e)
        raise HTTPException(status_code=502, detail="Invalid login token") from e
    return claims


# ── routes ──────────────────────────────────────────────────────────────


@router.get("/auth/login")
async def login():
    settings = get_settings()
    if not settings.oidc_configured:
        raise HTTPException(status_code=503, detail="Login is not configured")
    discovery = await _discover(settings.oidc_issuer)
    state = secrets.token_urlsafe(24)
    authorize = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect,
            "scope": "openid profile email",
            "state": state,
        }
    )
    response = RedirectResponse(
        f"{discovery['authorization_endpoint']}?{authorize}"
    )
    response.set_cookie(
        "epidash_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https"),
        path="/",
    )
    return response


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    settings = get_settings()
    if not settings.oidc_configured:
        raise HTTPException(status_code=503, detail="Login is not configured")
    expected_state = request.cookies.get("epidash_oauth_state")
    if not state or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid login state")
    claims = await exchange_code(code)
    sub = claims.get("sub", "")
    if not sub:
        raise HTTPException(status_code=502, detail="Token missing subject")
    email = claims.get("email", "")
    # Only existing EpidBot accounts may sign in. The probe fails closed:
    # without EpidBot's confirmation no session is issued.
    if get_settings().epidbot_user_validation and not await epidbot_user_exists(
        sub, email
    ):
        logger.info(
            "Login rejected: no EpidBot account for sub=%s email=%s",
            sub, email or "(none)",
        )
        raise HTTPException(
            status_code=403, detail="Only EpidBot users can sign in"
        )
    viewer = get_store().upsert_viewer(
        sub=sub,
        name=claims.get("preferred_username") or claims.get("name") or "",
        email=claims.get("email") or "",
    )
    response = RedirectResponse(f"{settings.base_url.rstrip('/')}/#/")
    response.delete_cookie("epidash_oauth_state", path="/")
    set_session_cookie(response, viewer["id"])
    return response


@router.get("/auth/logout")
async def logout():
    response = RedirectResponse(f"{get_settings().base_url.rstrip('/')}/#/")
    clear_session_cookie(response)
    return response


@router.get("/auth/me")
async def me(request: Request) -> dict:
    viewer = get_viewer_from_request(request)
    return {
        "viewer": {"name": viewer["name"], "email": viewer["email"]}
        if viewer
        else None,
        "login_enabled": get_settings().oidc_configured,
    }
