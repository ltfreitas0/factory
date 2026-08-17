#!/usr/bin/env python3
"""Create factory-only Cloudflare resources. Never touches jarvis* names."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ACCT = "8aeff78671e78879236d2fefbebe63b1"
ZONE = "3f0f69efd94b995f088166faaf56fbf7"
IDP = "1b3d209d-bbf5-4b91-bc23-fadccb64c40a"
PROTECTED = ("jarvis", "jarvis-api", "jarvis.henosis.cc", "jarvis-api.henosis.cc")


def _tokens() -> tuple[str, str]:
    text = Path.home().joinpath("base/sensitive/cloudflare-creds.txt").read_text().splitlines()

    def after(label: str) -> str:
        for i, line in enumerate(text):
            if label in line:
                for j in range(i, min(i + 15, len(text))):
                    if text[j].startswith("cfat_"):
                        return text[j].strip()
        raise SystemExit(f"missing token for {label}")

    return after("main-acc-all"), after("henosis-cc-full")


def req(method: str, url: str, token: str, data=None):
    body = None if data is None else json.dumps(data).encode()
    r = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise SystemExit(f"{method} {url} -> {exc.code} {detail}") from exc


def guard(name: str) -> None:
    n = (name or "").lower()
    if n.startswith("jarvis") or n in PROTECTED:
        raise SystemExit(f"refusing to touch protected name: {name}")


def main() -> None:
    acc, zone_tok = _tokens()
    tunnels = req("GET", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/cfd_tunnel?is_deleted=false", acc)
    tunnel = next((t for t in tunnels.get("result") or [] if t.get("name") == "factory"), None)
    if not tunnel:
        created = req(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/cfd_tunnel",
            acc,
            {"name": "factory", "config_src": "cloudflare"},
        )
        tunnel = created["result"]
        print("tunnel created", tunnel["id"])
    else:
        print("tunnel exists", tunnel["id"])
    tid = tunnel["id"]
    token = req("GET", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/cfd_tunnel/{tid}/token", acc)
    tunnel_token = token.get("result") if isinstance(token.get("result"), str) else (token.get("result") or {}).get("token")
    if not tunnel_token:
        raise SystemExit("no tunnel token")
    Path("/tmp/factory-tunnel.token").write_text(tunnel_token)
    Path("/tmp/factory-tunnel.token").chmod(0o600)

    cfg = {
        "config": {
            "ingress": [
                {
                    "hostname": "factory-api.henosis.cc",
                    "service": "http://127.0.0.1:8051",
                    "originRequest": {"connectTimeout": 30, "keepAliveTimeout": 90},
                },
                {"service": "http_status:404"},
            ]
        }
    }
    req("PUT", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/cfd_tunnel/{tid}/configurations", acc, cfg)
    print("tunnel ingress factory-api.henosis.cc -> 127.0.0.1:8051")

    dns = req("GET", f"https://api.cloudflare.com/client/v4/zones/{ZONE}/dns_records?name=factory-api.henosis.cc", zone_tok)
    if not (dns.get("result") or []):
        guard("factory-api.henosis.cc")
        rec = req(
            "POST",
            f"https://api.cloudflare.com/client/v4/zones/{ZONE}/dns_records",
            zone_tok,
            {
                "type": "CNAME",
                "name": "factory-api",
                "content": f"{tid}.cfargotunnel.com",
                "proxied": True,
                "ttl": 1,
            },
        )
        print("dns created", rec["result"]["name"])
    else:
        rec = dns["result"][0]
        guard(rec["name"])
        if "jarvis" in rec.get("content", ""):
            raise SystemExit("factory-api unexpectedly points at jarvis")
        print("dns exists", rec["name"])

    pages = req("GET", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/pages/projects", acc)
    if not any(p.get("name") == "factory" for p in pages.get("result") or []):
        req(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/pages/projects",
            acc,
            {"name": "factory", "production_branch": "main"},
        )
        print("pages project factory created")
    else:
        print("pages project factory exists")

    try:
        req(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/pages/projects/factory/domains",
            acc,
            {"name": "factory.henosis.cc"},
        )
        print("pages domain factory.henosis.cc attached")
    except SystemExit as exc:
        if "already" in str(exc).lower() or "409" in str(exc) or "400" in str(exc):
            print("pages domain already attached (or pending)")
        else:
            raise

    apps = req("GET", f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/access/apps", acc)
    if not any((a.get("domain") or "") == "factory.henosis.cc" for a in apps.get("result") or []):
        created = req(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/access/apps",
            acc,
            {
                "name": "Factory",
                "domain": "factory.henosis.cc",
                "type": "self_hosted",
                "session_duration": "24h",
                "allowed_idps": [IDP],
                "auto_redirect_to_identity": False,
                "destinations": [{"type": "public", "uri": "factory.henosis.cc"}],
            },
        )
        aid = created["result"]["id"]
        req(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/access/apps/{aid}/policies",
            acc,
            {
                "name": "Allow Lucas",
                "decision": "allow",
                "include": [{"email": {"email": "lucasltfbiz@gmail.com"}}],
            },
        )
        print("access app Factory created")
    else:
        print("access app Factory exists")

    # final safety: jarvis records still jarvis
    check = req("GET", f"https://api.cloudflare.com/client/v4/zones/{ZONE}/dns_records?per_page=100", zone_tok)
    for rec in check.get("result") or []:
        if rec["name"] in {"jarvis.henosis.cc", "jarvis-api.henosis.cc"}:
            print("untouched", rec["name"], rec["content"][:50])


if __name__ == "__main__":
    sys.exit(main())
