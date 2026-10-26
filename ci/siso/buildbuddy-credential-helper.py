#!/usr/bin/env python3
"""Host-scoped BuildBuddy REAPI credential helper for Chromium Siso.

Implements the standard credential-helper get protocol. It only returns the
BuildBuddy API key for the secure BuildBuddy REAPI host. The key is read from
the protected BUILDBUDDY_API_KEY environment variable and is never logged.
"""

import json
import os
import sys
from urllib.parse import urlsplit

_ALLOWED_HOST = "remote.buildbuddy.io"
_ALLOWED_SCHEMES = frozenset({"https", "grpcs"})
_ALLOWED_PORT = 443


def _fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _authorized_uri(uri: object) -> bool:
    if not isinstance(uri, str) or not uri:
        return False
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError:
        return False

    return (
        parsed.scheme.lower() in _ALLOWED_SCHEMES
        and parsed.hostname == _ALLOWED_HOST
        and port in (None, _ALLOWED_PORT)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["get"]:
        return _fail("Usage: buildbuddy-credential-helper.py get")

    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _fail("Invalid credential-helper JSON request")

    if not isinstance(request, dict) or not _authorized_uri(request.get("uri")):
        return _fail("Credential request URI is not an approved secure BuildBuddy URI")

    key = os.environ.get("BUILDBUDDY_API_KEY", "")
    if (
        not key
        or key != key.strip()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in key)
    ):
        return _fail("BUILDBUDDY_API_KEY is missing or invalid")

    response = {"headers": {"x-buildbuddy-api-key": [key]}}
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
