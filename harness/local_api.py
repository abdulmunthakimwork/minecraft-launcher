"""Local stand-ins for the franchise services the launcher UI talks to.

The real launcher resolves service roots through native discovery and then
talks to Minecraft/Microsoft endpoints. This harness resolves every one of
them to a local path (see ``SERVICE_ROOTS``) and answers here, so the whole
application runs with no network at all.

Each handler returns ``(status, payload)``. The shapes are the smallest valid
document the UI's reducers and selectors accept: an empty collection where the
UI maps over a list, and an explicit 404 only where an empty document would be
ambiguous.
"""
import json
import re

# Service name (from the launcher's own `Ky` enum) -> local path root.
# These are returned by the host's `getFranchiseServiceUri` and also replace the
# FallbackDiscoverableServices table in the bundles.
SERVICE_ROOTS = {
    "VOTING": "/api/voting",
    "AUTH": "/api/auth",
    "REALMS_BEDROCK": "/api/realms/bedrock",
    "REALMS_JAVA": "/api/realms/java",
    "REALMS_PAYMENTS": "/api/realms/payments",
    "MARKETPLACE_ENTITLEMENTS": "/api/entitlements",
    "MARKETPLACE_STORE": "/api/store",
    "MESSAGING": "/api/messaging",
}

# An empty marketplace catalogue page. The store chrome renders from this and
# simply has no items to show, which is the honest offline state.
_EMPTY_PAGE = {
    "id": "marketplace",
    "title": "Marketplace",
    "sections": [],
    "rows": [],
    "items": [],
    "pagination": {"hasMore": False, "continuationToken": None},
    "totalCount": 0,
}

# The inbox query reads `result.inboxSummary.categories` before it maps, so the
# empty document has to carry that shape rather than a bare object.
_EMPTY_INBOX = {
    "result": {
        "inboxSummary": {"categories": [], "unreadCount": 0},
        "messages": [],
        "sessionId": None,
    }
}

# Entitlements: the launcher reads `items` and expects each entry to carry a
# product id, so an empty list is the correct offline answer.
_EMPTY_ENTITLEMENTS = {"items": [], "signatures": [], "signature": None}

# Realms: several reducers read `worlds` / `servers` and the result wrapper.
_EMPTY_WORLDS = {
    "servers": [],
    "worlds": [],
    "result": {"worlds": [], "servers": [], "subscriptions": []},
}


def _json(status, payload):
    return status, payload


# Each rule is (regex, payload). The regex is matched against the request
# path with the service root prefix removed, so one table serves every root.
_RULES = (
    # -- marketplace store ------------------------------------------------
    (r"^/v1\.0/catalog/web/.*$", _EMPTY_PAGE),
    (r"^/v1\.0/catalog/.*$", _EMPTY_PAGE),
    (r"^/catalog/.*$", _EMPTY_PAGE),
    (r"^/store/.*$", _EMPTY_PAGE),
    # -- inbox messaging --------------------------------------------------
    (r"^/api/v1\.0/session/start.*$", _EMPTY_INBOX),
    (r"^/api/v1\.0/messages/.*$", {"result": {"messages": [], "categories": []}}),
    (r"^/v1\.0/messages/.*$", {"result": {"messages": [], "categories": []}}),
    (r"^/messages/.*$", {"result": {"messages": [], "categories": []}}),
    # -- entitlements -----------------------------------------------------
    (r"^/entitlements(/.*)?$", _EMPTY_ENTITLEMENTS),
    (r"^/api/v1\.0/entitlements(/.*)?$", _EMPTY_ENTITLEMENTS),
    # -- realms -----------------------------------------------------------
    (r"^/(mco|worlds|invites|subscriptions)(/.*)?$", _EMPTY_WORLDS),
    (r"^/api/v1\.0/(mco|worlds|invites|subscriptions)(/.*)?$", _EMPTY_WORLDS),
    # -- discovery --------------------------------------------------------
    (r"^/api/v1\.0/discovery.*$", {"items": [], "expiresOn": None}),
    (r"^/discovery.*$", {"items": [], "expiresOn": None}),
    # -- voting -----------------------------------------------------------
    (r"^/v1\.0/votes?.*$", {"items": [], "votes": []}),
    (r"^/votes?$", {"items": [], "votes": []}),
    # -- identity ---------------------------------------------------------
    # Signing in needs a real Microsoft account; a 401 is the honest answer
    # and lets the UI show its own "cannot sign in" path.
    (r"^/authorization(/.*)?$", {"error": "sign-in requires network access"}),
)


def respond(path):
    """Answer one request for a locally hosted franchise service."""
    path = path.split("?", 1)[0]
    tail = path
    for root in SERVICE_ROOTS.values():
        if tail.startswith(root):
            tail = tail[len(root):]
            break
    if not tail.startswith("/"):
        tail = "/" + tail

    for pattern, payload in _RULES:
        if re.match(pattern, tail):
            return _json(200, payload)

    # Anything else is a service the app can function without; answer with an
    # empty document instead of a transport error so no retry loop starts.
    return _json(200, {})


def dump(payload):
    return json.dumps(payload).encode("utf-8")
