#!/usr/bin/env python3
# One-time helper — obtain a Google Meet OAuth refresh token for the
# single-account meetings integration.
#
# Prerequisites (one-time, in Google Cloud Console):
#   * A project with the Google Meet API enabled.
#   * An OAuth 2.0 Client ID of type "Web application".
#   * `http://localhost` registered as an Authorized redirect URI on it.
#
# Usage:
#   uv run python scripts/get_meet_refresh_token.py \
#       --client-id XXX --client-secret YYY
#   # or set GOOGLE_MEET_CLIENT_ID / GOOGLE_MEET_CLIENT_SECRET in the env.
#
# Paste the printed GOOGLE_MEET_REFRESH_TOKEN into the backend env
# (alongside GOOGLE_MEET_CLIENT_ID / GOOGLE_MEET_CLIENT_SECRET). Run once
# per deployment — the refresh token is long-lived.

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

import httpx

# Google Meet REST API v2 scopes — create spaces + read conference records.
_SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
]
_REDIRECT_URI = "http://localhost"


def main() -> int:
    ap = argparse.ArgumentParser(description="Obtain a Google Meet OAuth refresh token.")
    ap.add_argument("--client-id", default=os.environ.get("GOOGLE_MEET_CLIENT_ID", ""))
    ap.add_argument("--client-secret", default=os.environ.get("GOOGLE_MEET_CLIENT_SECRET", ""))
    args = ap.parse_args()
    if not args.client_id or not args.client_secret:
        print(
            "ERROR: pass --client-id / --client-secret or set "
            "GOOGLE_MEET_CLIENT_ID / GOOGLE_MEET_CLIENT_SECRET.",
            file=sys.stderr,
        )
        return 2

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(_SCOPES),
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # force the consent screen so one is issued
        }
    )
    print("\n1. Open this URL in a browser and grant access:\n")
    print(f"   {auth_url}\n")
    print("2. Google redirects to a 'localhost' page that will not load —")
    print("   that is expected. Copy the `code=` value from the address bar.\n")
    # The browser address bar usually shows the code percent-encoded
    # (the leading "4/" becomes "4%2F"). unquote handles both forms.
    code = urllib.parse.unquote(input("Paste the code here: ").strip())
    if not code:
        print("ERROR: no code provided.", file=sys.stderr)
        return 2

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "redirect_uri": _REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: token exchange failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1
    refresh = resp.json().get("refresh_token")
    if not refresh:
        print(
            "ERROR: no refresh_token in the response. Revoke the app's prior "
            "access at https://myaccount.google.com/permissions and retry — "
            "Google only issues a refresh token when the consent screen shows.",
            file=sys.stderr,
        )
        return 1
    print("\n[OK] Add this to your backend env:\n")
    print(f"   GOOGLE_MEET_REFRESH_TOKEN={refresh}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
