"""A stand-in for `cloudflared` + Cloudflare Access, in front of the app.

It reproduces the parts the app actually depends on, faithfully:

  * every proxied request gains `CF-Connecting-IP`, which is how the app knows
    it arrived through the tunnel rather than off the LAN;
  * a browser with a valid Access session cookie also gains
    `Cf-Access-Authenticated-User-Email`;
  * a **service token** (CF-Access-Client-Id / -Secret) is let through but does
    NOT get that email header -- Cloudflare puts `common_name` in the JWT for
    service tokens, not `email`. This is the distinction that decides whether
    the bridge can post data but not download the setup bundle;
  * anything else is bounced at the edge with a 302 to the login page, exactly
    as Access does, so the app never sees the request at all.
"""
from __future__ import annotations

import os
import sys

import httpx
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

ORIGIN = os.environ["TUNNEL_ORIGIN"]
SERVICE_ID = os.environ.get("ACCESS_SERVICE_ID", "svc.id")
SERVICE_SECRET = os.environ.get("ACCESS_SERVICE_SECRET", "svc.secret")
SESSION_COOKIE = os.environ.get("ACCESS_COOKIE", "CF_Authorization")
USER_EMAIL = os.environ.get("ACCESS_USER_EMAIL", "gm.other.info@gmail.com")
HOSTNAME = os.environ.get("TUNNEL_HOSTNAME", "whoop.example.com")

client = httpx.AsyncClient(base_url=ORIGIN, timeout=60.0)
HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive",
              "transfer-encoding", "upgrade"}


async def proxy(request):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}

    has_service_token = (headers.get("cf-access-client-id") == SERVICE_ID
                         and headers.get("cf-access-client-secret") == SERVICE_SECRET)
    has_session = request.cookies.get(SESSION_COOKIE) == "valid-session"

    if not (has_service_token or has_session):
        # Access bounces this at the edge; the origin never sees it.
        return Response(status_code=302, headers={
            "location": f"https://example.cloudflareaccess.com/cdn-cgi/access/login/{HOSTNAME}"})

    headers["cf-connecting-ip"] = "203.0.113.7"          # a public client IP
    headers["x-forwarded-proto"] = "https"
    headers["x-forwarded-host"] = HOSTNAME
    headers["host"] = HOSTNAME
    if has_session:
        headers["cf-access-authenticated-user-email"] = USER_EMAIL
    # A service token deliberately does NOT get the email header.

    body = await request.body()
    upstream = await client.request(request.method, request.url.path,
                                    params=dict(request.query_params),
                                    content=body, headers=headers)
    out = {k: v for k, v in upstream.headers.items()
           if k.lower() not in ("content-length", "transfer-encoding", "connection")}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out)


app = Starlette(routes=[Route("/{path:path}", proxy,
                              methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])])
