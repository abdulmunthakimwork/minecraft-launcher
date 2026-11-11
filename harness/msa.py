"""Microsoft account sign-in for the launcher (optional, online only).

The offline account path (`server._offline_record`) never talks to anything;
this module is the other half: a genuine Microsoft personal-account sign-in
for users who prefer online accounts. Offline play never requires it.

Flow (public-client authorization code + PKCE, no client secret):

    browser -> login.microsoftonline.com/consumers (authorize, PKCE)
            -> http://localhost:<port>/__msa/callback   (this host)
    host    -> redeem the code for a Microsoft access token
            -> Xbox User Token (user.auth.xboxlive.com)
            -> XSTS token      (xsts.auth.xboxlive.com, rp: api.minecraftservices.com)
            -> Minecraft Services token (api.minecraftservices.com)
            -> entitlements/mcstore (is Java owned?) and minecraft/profile

The redirect lands back on this very server, so no second listener is needed;
Electron opens the authorize URL in the system browser and the launcher page
polls `/__msa/status` until the callback completes.

Setup (documented in harness/README.md): the user registers a public client
ID in their own Entra tenant (personal accounts, redirect `http://localhost`)
and puts the *client id* -- never a secret -- in the environment variable
LAUNCHER_MSA_CLIENT_ID or in `<data dir>/microsoft-client-id.txt`. Without it
every entry point fails with an honest setup message; nothing is faked.

Tokens are kept out of the renderer: the page only ever sees the account
record the launcher needs. On Windows the token store is encrypted with
DPAPI (user scope); elsewhere it falls back to a 0600 file and says so.

Caveat worth knowing (see harness/README.md): Microsoft publishes the Xbox
token exchange, but the Minecraft Services acceptance of a third-party
client id is not covered by public Mojang documentation. If Minecraft
Services rejects this app's registration, sign-in reports that rejection
verbatim -- it is never worked around by borrowing another launcher's id.
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
XBL_USER = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTHORIZE = "https://xsts.auth.xboxlive.com/xsts/authorize"
MCS = "https://api.minecraftservices.com"
SCOPES = "openid profile offline_access xboxlive.signin xboxlive.offline_access"
AGENT = "minecraft-launcher-harness/1.0 (local desktop shell)"
PENDING_TTL = 600          # seconds a browser round trip may take
EXPIRY_SLACK = 120         # refresh this many seconds before a token dies
# The entitlement names Minecraft Services uses for Java ownership.
JAVA_ENTITLEMENTS = ("game_minecraft", "product_minecraft")

_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()
_GENERATION = 0            # invalidates work started before logout/replacement
_STORE_PATH = None
_CLIENT_ID_FILE = None
_CLIENT_ID = None          # resolved once; None means "not configured"
_STORAGE_ERROR = None
_STATE = {
    "phase": "idle",       # idle | waiting | completing | done | error
    "error": None,
    "account": None,       # safe summary for the UI (no tokens)
}
_PENDING = None            # {verifier, state, redirect, at}


def configure(data_dir):
    """Point the module at the writable data folder."""
    global _STORE_PATH, _CLIENT_ID_FILE, _CLIENT_ID, _STORAGE_ERROR, _GENERATION, _PENDING
    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    store_path = os.path.join(data_dir, "msa_tokens.json")
    with _LOCK:
        if store_path != _STORE_PATH:
            _GENERATION += 1
            _PENDING = None
            _CLIENT_ID = None
            _STORAGE_ERROR = None
            _STATE.update({"phase": "idle", "error": None, "account": None})
        _STORE_PATH = store_path
        _CLIENT_ID_FILE = os.path.join(data_dir, "microsoft-client-id.txt")


def client_id():
    """The configured public client id, or None. A client id is public data."""
    global _CLIENT_ID
    with _LOCK:
        if _CLIENT_ID:
            return _CLIENT_ID
        _CLIENT_ID = _client_configuration()["clientId"]
        return _CLIENT_ID


def _public_client_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return None
    return value.lower()


def _client_configuration():
    """Read only public application configuration, never the session store."""
    value = (os.environ.get("LAUNCHER_MSA_CLIENT_ID") or "").strip()
    source = "environment" if value else None
    if not value and _CLIENT_ID_FILE:
        try:
            with open(_CLIENT_ID_FILE, encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                source = "file"
        except OSError:
            value = ""
        except UnicodeError:
            value, source = "", "file"
    cid = _public_client_id(value)
    return {"configured": bool(cid), "clientId": cid, "source": source,
            "environmentOverride": source == "environment"}


def configuration():
    """Safe setup status for the renderer; the application ID is public."""
    global _CLIENT_ID
    with _LOCK:
        result = _client_configuration()
        _CLIENT_ID = result["clientId"]
        return result


class ConfigurationBusy(ValueError):
    pass


def save_client_id(value):
    """Atomically save a public GUID without modifying accounts or sessions."""
    global _CLIENT_ID
    cid = _public_client_id(value)
    if not cid:
        raise ValueError("Enter a valid application (client) ID in GUID format.")
    with _LOCK:
        _expire_pending_locked()
        if _STATE["phase"] in ("waiting", "completing"):
            raise ConfigurationBusy("Finish the Microsoft sign-in already in progress before changing its application ID.")
        if not _CLIENT_ID_FILE:
            raise ValueError("The launcher's Microsoft configuration folder is unavailable.")
        temp = None
        try:
            descriptor, temp = tempfile.mkstemp(prefix=".msa-client-id-", suffix=".tmp",
                                               dir=os.path.dirname(_CLIENT_ID_FILE))
            with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
                fh.write(cid + "\n")
            os.replace(temp, _CLIENT_ID_FILE)
            _CLIENT_ID = None
            result = _client_configuration()
            _CLIENT_ID = result["clientId"]
            return result
        finally:
            if temp and os.path.exists(temp):
                os.remove(temp)


SETUP_MESSAGE = (
    "Microsoft sign-in is not configured. Register a public application "
    "client id (personal Microsoft accounts, redirect URI http://localhost) "
    "and enter it in the launcher's Microsoft sign-in setup, or put it in "
    "LAUNCHER_MSA_CLIENT_ID or microsoft-client-id.txt inside the launcher's "
    "data folder. See harness/README.md."
)


def configured():
    return bool(client_id())


def storage_kind():
    # A legacy plaintext file remains plaintext until migration succeeds.
    if _STORE_PATH:
        try:
            with open(_STORE_PATH, encoding="utf-8") as fh:
                document = json.load(fh)
            if isinstance(document, dict):
                if "dpapi" in document:
                    return "dpapi"
                if "plain" in document:
                    return "plain"
        except (OSError, ValueError):
            pass
    if _STORAGE_ERROR:
        return "unavailable"
    return "dpapi" if sys.platform == "win32" else "plain"


# --- protected token store -------------------------------------------------
def _dpapi(data, decrypt=False):
    """Use user-scoped DPAPI with pointer-safe signatures on 64-bit Windows."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

        buf = ctypes.create_string_buffer(data, len(data))
        inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
        out = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        crypt = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        crypt.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                          ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt.restype = wintypes.BOOL
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        if not crypt(
            ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)
        ):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            free(out.pbData)
    except Exception:
        return None


def _protect(data):
    return _dpapi(data)


def _unprotect(blob):
    return _dpapi(blob, decrypt=True)


def _save_store(payload):
    """Persist the session. Encrypted on Windows; 0600 file elsewhere."""
    global _STORAGE_ERROR
    if not _STORE_PATH:
        raise ValueError("Microsoft session storage is not configured")
    raw = json.dumps(payload).encode("utf-8")
    blob = _protect(raw)
    if sys.platform == "win32" and not blob:
        _STORAGE_ERROR = "Windows could not protect the Microsoft session with DPAPI"
        raise ValueError(_STORAGE_ERROR)
    doc = ({"v": 1, "dpapi": base64.b64encode(blob).decode("ascii")} if blob
           else {"v": 1, "plain": payload})
    temp = None
    try:
        descriptor, temp = tempfile.mkstemp(prefix=".msa-", suffix=".tmp",
                                           dir=os.path.dirname(_STORE_PATH))
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.chmod(temp, 0o600)
        os.replace(temp, _STORE_PATH)
        _STORAGE_ERROR = None
    except OSError as exc:
        _STORAGE_ERROR = "Microsoft session could not be saved: %s" % exc
        raise
    finally:
        if temp and os.path.exists(temp):
            os.remove(temp)


def _load_store():
    if not _STORE_PATH:
        return None
    try:
        with open(_STORE_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if "dpapi" in doc:
        try:
            raw = _unprotect(base64.b64decode(doc["dpapi"], validate=True))
        except Exception:
            raw = None
        if not raw:
            # Tokens written on another machine or another user: they cannot
            # be decrypted here, so the session is simply signed out.
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    payload = doc.get("plain")
    if not isinstance(payload, dict):
        return None
    if sys.platform == "win32":
        try:
            _save_store(payload)
        except (OSError, ValueError):
            # Keep readable legacy sessions intact when migration is unavailable.
            pass
    return payload


def _forget_store():
    if not _STORE_PATH:
        return
    try:
        os.remove(_STORE_PATH)
    except FileNotFoundError:
        pass


# --- http helpers ----------------------------------------------------------
def _post_json(url, payload, headers=None, timeout=30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": AGENT,
                 **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _post_form(url, payload, timeout=30):
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _get_json(url, token, timeout=30):
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "User-Agent": AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# --- oauth -----------------------------------------------------------------
def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _expire_pending_locked():
    global _PENDING, _GENERATION
    if _PENDING and time.time() - _PENDING["at"] > PENDING_TTL:
        _PENDING = None
        _GENERATION += 1
        _STATE.update({"phase": "error", "error": "the sign-in request expired"})


def sign_in(server_port):
    """Build the authorize URL for the system browser. Raises ValueError."""
    global _PENDING
    with _LOCK:
        _expire_pending_locked()
        cid = client_id()
        if not cid:
            raise ValueError(SETUP_MESSAGE)
        if _STATE["phase"] in ("waiting", "completing"):
            raise ValueError("a Microsoft sign-in is already in progress")
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(32)
        redirect = "http://localhost:%d/__msa/callback" % int(server_port)
        _PENDING = {
            "verifier": verifier,
            "state": state,
            "redirect": redirect,
            "at": time.time(),
            "client_id": cid,
            "generation": _GENERATION,
        }
        _STATE.update({"phase": "waiting", "error": None})
    params = urllib.parse.urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_mode": "query",
    })
    return AUTH + "/authorize?" + params


def _redeem(pending, code):
    doc = _post_form(AUTH + "/token", {
        "client_id": pending["client_id"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect"],
        "code_verifier": pending["verifier"],
        "scope": SCOPES,
    })
    if not doc.get("access_token"):
        raise ValueError("Microsoft did not return an access token: %s" % _brief(doc))
    return doc


def _refresh(pending_or_cid, refresh_token):
    cid = pending_or_cid if isinstance(pending_or_cid, str) else pending_or_cid["client_id"]
    doc = _post_form(AUTH + "/token", {
        "client_id": cid,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPES,
    })
    if not doc.get("access_token"):
        raise ValueError("Microsoft refused the refresh token: %s" % _brief(doc))
    doc.setdefault("refresh_token", refresh_token)
    return doc


def _brief(doc):
    # Unexpected response objects can contain credentials; never echo them.
    if not isinstance(doc, dict):
        return "unexpected service response"
    return str(doc.get("error_description") or doc.get("error")
               or "unexpected service response")[:300]


_XSTS_ERRORS = {
    2148916233: "This Microsoft account is a child account and cannot sign in to Xbox.",
    2148916235: "This account is not allowed to play on Xbox.",
    2148916238: "This account needs adult verification on Xbox before it can sign in.",
}


def _xbox_chain(ms_access_token):
    """Microsoft access token -> Xbox user token -> XSTS -> Minecraft token."""
    user = _post_json(XBL_USER, {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": "d=" + ms_access_token,
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    })
    user_token = user.get("Token")
    uhs = (((user.get("DisplayClaims") or {}).get("xui") or [{}])[0]).get("uhs")
    if not user_token or not uhs:
        raise ValueError("Xbox did not accept this account: %s" % _brief(user))
    xsts = _post_json(XSTS_AUTHORIZE, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [user_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",
    })
    xsts_token = xsts.get("Token")
    if not xsts_token:
        xerr = ((xsts.get("XErr")))
        try:
            xerr = int(xerr)
        except (TypeError, ValueError):
            xerr = 0
        raise ValueError(_XSTS_ERRORS.get(
            xerr, "Xbox could not authorize this account: %s" % _brief(xsts)))
    mcs = _post_json(MCS + "/authentication/login_with_xbox", {
        "identityToken": "XBL3.0 x=%s;%s" % (uhs, xsts_token),
    })
    access_token = mcs.get("access_token")
    if not access_token:
        raise ValueError("Minecraft Services refused this account: %s" % _brief(mcs))
    return {
        "access_token": access_token,
        "expires_in": int(mcs.get("expires_in") or 86400),
        "xuid": (((mcs.get("DisplayClaims") or {}).get("xui") or [{}])[0]).get("xid") or "",
    }


def _entitlements(mc_token):
    doc = _get_json(MCS + "/entitlements/mcstore", mc_token)
    items = doc.get("items") if isinstance(doc, dict) else None
    names = {str(i.get("name") or "") for i in items or [] if isinstance(i, dict)}
    return sorted(n for n in names if n in JAVA_ENTITLEMENTS)


def _profile(mc_token):
    doc = _get_json(MCS + "/minecraft/profile", mc_token)
    ident = str(doc.get("id") or "")
    name = str(doc.get("name") or "")
    if not ident or not name:
        raise ValueError(
            "This Microsoft account has no Minecraft profile. Sign in with the "
            "account that owns Minecraft Java Edition."
        )
    return {"id": ident, "name": name}


def callback(code, state):
    """Finish the flow the browser redirect started. Raises ValueError."""
    global _PENDING, _GENERATION
    with _LOCK:
        _expire_pending_locked()
        pending = _PENDING
        if not pending:
            raise ValueError("the Microsoft sign-in request expired; try again")
        if not state or state != pending["state"]:
            raise ValueError("the Microsoft sign-in response did not match this request")
        if _STATE["phase"] != "waiting":
            raise ValueError("this Microsoft sign-in response is already being processed")
        _STATE.update({"phase": "completing", "error": None})
    try:
        tokens = _redeem(pending, code)
        chain = _xbox_chain(tokens["access_token"])
        mc_token = chain["access_token"]
        profile = _profile(mc_token)
        owned = _entitlements(mc_token)
        payload = {
            "refresh_token": tokens.get("refresh_token") or "",
            "access_token": mc_token,
            "expires_at": time.time() + chain["expires_in"] - EXPIRY_SLACK,
            "client_id": pending["client_id"],
            "xuid": chain["xuid"],
            "profile": profile,
            "entitlements": owned,
        }
        with _LOCK:
            if _PENDING is not pending or pending["generation"] != _GENERATION:
                raise ValueError("the Microsoft sign-in request was cancelled; try again")
            _save_store(payload)
            _GENERATION += 1
            _PENDING = None
            _STATE.update({"phase": "done", "error": None,
                           "account": _summary(profile, owned)})
        return payload
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = _brief(json.load(exc))
            except Exception:
                detail = str(exc)
            message = "Microsoft sign-in failed (HTTP %s): %s" % (exc.code, detail)
        else:
            message = str(exc)
        with _LOCK:
            if _PENDING is pending and pending["generation"] == _GENERATION:
                _STATE.update({"phase": "error", "error": message})
            else:
                message = "the Microsoft sign-in request was cancelled; try again"
        raise ValueError(message) from exc
    finally:
        with _LOCK:
            if _PENDING is pending:
                _PENDING = None


def _summary(profile, entitlements):
    return {
        "name": profile.get("name"),
        "id": "msa-" + str(profile.get("id") or ""),
        "ownsJava": bool(entitlements),
    }


def _valid_payload(payload):
    if not isinstance(payload, dict):
        return False
    profile = payload.get("profile")
    expiry = payload.get("expires_at")
    owned = payload.get("entitlements", [])
    return (isinstance(profile, dict)
            and isinstance(profile.get("id"), str) and bool(profile["id"])
            and isinstance(profile.get("name"), str) and bool(profile["name"])
            and isinstance(payload.get("access_token"), str) and bool(payload["access_token"])
            and isinstance(expiry, (int, float)) and 0 < expiry < float("inf")
            and isinstance(owned, list) and all(isinstance(ent, str) for ent in owned))


def _refreshed(payload, generation=None):
    """The stored session with a fresh access token when close to expiry."""
    if not _valid_payload(payload):
        return None
    if generation is None:
        with _LOCK:
            generation = _GENERATION
    if payload.get("expires_at", 0) > time.time() + EXPIRY_SLACK:
        return payload
    if not payload.get("refresh_token"):
        return payload if payload["expires_at"] > time.time() else None
    try:
        tokens = _refresh(payload.get("client_id") or client_id(), payload["refresh_token"])
        chain = _xbox_chain(tokens["access_token"])
        refreshed = dict(payload,
                         refresh_token=tokens.get("refresh_token") or payload["refresh_token"],
                         access_token=chain["access_token"],
                         expires_at=time.time() + chain["expires_in"] - EXPIRY_SLACK,
                         xuid=chain.get("xuid") or payload.get("xuid") or "")
        if not _valid_payload(refreshed):
            raise ValueError("Minecraft Services returned an unusable session")
        with _LOCK:
            if generation != _GENERATION:
                return None
            _save_store(refreshed)
        return refreshed
    except Exception:
        # A transient network failure must not sign the user out; an actually
        # expired session cannot be used, so that one is treated as signed out.
        with _LOCK:
            if generation != _GENERATION:
                return None
        return payload if payload["expires_at"] > time.time() else None


def _session_payload():
    # Serialize reads through refresh so a rotated refresh token is redeemed
    # only once. Network work never holds the state lock needed by logout.
    with _REFRESH_LOCK:
        with _LOCK:
            generation = _GENERATION
            payload = _load_store()
        payload = _refreshed(payload, generation)
        with _LOCK:
            return payload if generation == _GENERATION else None


def account_record():
    """The launcher-shaped account record, or None when not signed in."""
    payload = _session_payload()
    if not payload:
        return None
    profile = payload.get("profile") or {}
    ident = "msa-" + str(profile.get("id") or "")
    owned = sorted(payload.get("entitlements") or [])
    name = str(profile.get("name") or "Player")
    return {
        "id": ident,
        "localId": ident,
        "remoteId": str(profile.get("id") or ""),
        "type": "Microsoft",
        "offline": False,
        "persistent": True,
        "username": name,
        "gamertag": name,
        "javaProfileName": name,
        "accessToken": "",  # credentials stay in the host's game_identity()
        "accessTokenExpiresAt": None,
        "avatar": None,
        "entitlements": owned,
        "entitlementsSource": {ent: "RETAIL" for ent in owned},
        "licenseProductIds": [],
        "xbox": {"xuid": payload.get("xuid") or ""},
        "requiresInteractiveLogin": False,
        "eligibleForFreeTrials": False,
        "eligibleForMigration": False,
        "hasMultipleProfiles": False,
        "inForcedMigration": False,
        "legacy": False,
        "userProperites": [],
    }


def game_identity():
    """Name/uuid/token the game is launched as when an MSA account is active."""
    payload = _session_payload()
    if not payload:
        return None
    profile = payload.get("profile") or {}
    ident = str(profile.get("id") or "")
    digits = "".join(c for c in ident if c in "0123456789abcdefABCDEF").lower()
    return {
        "name": str(profile.get("name") or "Player"),
        "uuid": digits if len(digits) == 32 else ident,
        "access_token": payload.get("access_token") or "0",
        "xuid": str(payload.get("xuid") or "0"),
    }


def fail(message, state=None):
    """Record a rejection (e.g. by the identity provider) for the polling UI."""
    global _PENDING, _GENERATION
    with _LOCK:
        _expire_pending_locked()
        if not _PENDING or not state or state != _PENDING["state"]:
            raise ValueError("the Microsoft sign-in response did not match this request")
        _PENDING = None
        _GENERATION += 1
        _STATE.update({"phase": "error", "error": str(message)})


def logout():
    global _PENDING, _GENERATION, _STORAGE_ERROR
    with _LOCK:
        _GENERATION += 1
        _PENDING = None
        try:
            _forget_store()
        except OSError as exc:
            message = "Microsoft session could not be removed: %s" % exc
            _STORAGE_ERROR = message
            _STATE.update({"phase": "error", "error": message})
            raise ValueError(message) from exc
        _STORAGE_ERROR = None
        _STATE.update({"phase": "idle", "error": None, "account": None})


def reset_after_reload():
    """Keep the configured id and any signed-in session across a hot reload."""
    global _PENDING
    with _LOCK:
        _PENDING = None
        if _STATE["phase"] in ("waiting", "completing"):
            _STATE.update({"phase": "idle", "error": "the host restarted; try again"})
        _STATE["account"] = _summary_of_store()


def _summary_of_store():
    payload = _load_store()
    if not _valid_payload(payload) or payload["expires_at"] <= time.time():
        return None
    return _summary(payload.get("profile") or {}, payload.get("entitlements") or [])


def status():
    with _LOCK:
        _expire_pending_locked()
        snapshot = dict(_STATE)
        snapshot["account"] = _summary_of_store()
        snapshot["configured"] = configured()
        snapshot["storage"] = storage_kind() if snapshot["configured"] else None
        snapshot["storageError"] = _STORAGE_ERROR
    return snapshot
