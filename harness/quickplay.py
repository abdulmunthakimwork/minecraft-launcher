"""Source-bound installation history for the launcher's native Quick Play UI.

This module never scans worlds or writes game files. Its private entries retain
source references when an installation is temporarily unavailable.
"""
import base64
import copy
import datetime
import hashlib
import json
import re
import struct

SOURCES = ("Java", "Bedrock", "Launcher", "Unknown")
CUSTOM_FIELDS = ("name", "pinnedIndex", "letterGlyphVisible", "customizedIcon")


class QuickPlayError(ValueError):
    def __init__(self, message, status=400, code="invalid_quickplay"):
        super().__init__(message)
        self.status, self.code = status, code


def _state(value):
    if value is None:
        return {"entries": {}}
    if not isinstance(value, dict) or not isinstance(value.get("entries", {}), dict):
        raise QuickPlayError("Quick Play history is malformed; the saved data was preserved.", 500)
    if "hideUntracked" in value and type(value["hideUntracked"]) is not bool:
        raise QuickPlayError("Quick Play history has an invalid hide flag; the saved data was preserved.", 500)
    result = copy.deepcopy(value)
    result.setdefault("entries", {})
    if any(not isinstance(key, str) or not isinstance(entry, dict) for key, entry in result["entries"].items()):
        raise QuickPlayError("Quick Play history contains an invalid entry; the saved data was preserved.", 500)
    for entry in result["entries"].values():
        if (not isinstance(entry.get("configId"), str) or not isinstance(entry.get("context"), list)
                or any(not isinstance(root, str) for root in entry["context"])
                or not isinstance(entry.get("sources"), list)
                or any(not isinstance(ref, dict) or not isinstance(ref.get("source"), str)
                       or not isinstance(ref.get("id"), str) for ref in entry["sources"])
                or "hidden" in entry and type(entry["hidden"]) is not bool
                or "launchedAt" in entry and (type(entry["launchedAt"]) is not int or entry["launchedAt"] < 0)
                or "primarySource" in entry and entry["primarySource"] not in entry["sources"]
                or "profileKey" in entry and (not isinstance(entry["profileKey"], str)
                                               or not re.fullmatch(r"[a-f0-9]{64}", entry["profileKey"]))
                or not isinstance(entry.get("customization", {}), dict)):
            raise QuickPlayError("Quick Play history contains an invalid source binding; the saved data was preserved.", 500)
        try:
            _customization(entry.get("customization", {}))
        except QuickPlayError as exc:
            raise QuickPlayError("Quick Play history contains an invalid customization; the saved data was preserved. " + str(exc), 500) from exc
    return result


def _timestamp(value):
    if type(value) in (int, float):
        return int(value) if 0 < value < 253402300800000 else 0
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return max(0, int(parsed.timestamp() * 1000))
    except (ValueError, OverflowError, OSError):
        return 0


def same_binding(left, right):
    if "availableSources" in right and "profileKey" in left:
        return _match_score(right, left) is not None
    a, b = left.get("sources") or [], right.get("sources") or []
    if a or b:
        return bool(a and b and any(ref in b for ref in a))
    return left.get("configId") == right.get("configId") and left.get("context") == right.get("context")


def configuration_binding(configuration_id, record, context):
    sources = copy.deepcopy(record.get("sources") or [])
    available = copy.deepcopy(record.get("availableSources", sources))
    profile = {key: value for key, value in record["profile"].items() if key not in ("lastUsed", "lastPlayed")}
    result = {"configId": configuration_id, "sources": sources, "availableSources": available, "context": list(context),
              "profileKey": hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()}
    if available:
        result["primarySource"] = available[0]
    return result


def _match_score(binding, entry):
    sources, known = binding["sources"], entry.get("sources") or []
    if not sources and not known:
        if binding["configId"] != entry.get("configId") or binding["context"] != entry.get("context"):
            return None
        return (True, True, True, 0, _timestamp(entry.get("launchedAt")))
    if not sources or not known or not any(ref in known for ref in sources):
        return None
    available = binding.get("availableSources", sources)
    primary = entry.get("primarySource", known[0])
    primary_present = primary in available
    # Historical mirror references alone cannot transfer customized history.
    # A missing primary may use a mirror only while its profile still agrees.
    if not primary_present and (not entry.get("profileKey") or entry["profileKey"] != binding.get("profileKey")):
        return None
    return (bool(available and available[0] == primary), primary_present,
            binding["configId"] == entry.get("configId"), sum(ref in known for ref in available),
            _timestamp(entry.get("launchedAt")))


def _new_identity(binding, reserved=()):
    seed = binding.get("primarySource") or {"configId": binding["configId"], "context": binding["context"]}
    raw = json.dumps(seed, sort_keys=True)
    ident = "java-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    suffix = 0
    while ident in reserved:
        suffix += 1
        ident = "java-" + hashlib.sha256((raw + "\0" + binding["configId"] + "\0" + str(suffix)).encode("utf-8")).hexdigest()
    return ident


def _identity(binding, entries):
    matches = [(score, ident) for ident, entry in entries.items() if (score := _match_score(binding, entry)) is not None]
    if matches:
        return max(matches)[1]
    return _new_identity(binding, entries)


def _assign_identities(bindings, entries):
    candidates = [(score, ident, config_id) for config_id, binding in bindings.items()
                  for ident, entry in entries.items() if (score := _match_score(binding, entry)) is not None]
    assigned, used = {}, set()
    for _, ident, config_id in sorted(candidates, reverse=True):
        if config_id not in assigned and ident not in used:
            assigned[config_id] = ident
            used.add(ident)
    # Reserve unavailable entries too: a newly split source must never obtain
    # another installation's historical ID merely because its hash agrees.
    reserved = set(entries) | used
    for config_id in sorted(bindings):
        if config_id not in assigned:
            assigned[config_id] = _new_identity(bindings[config_id], reserved)
            reserved.add(assigned[config_id])
    return assigned


def _snapshot(configurations, value, context, legacy_cleared=False):
    state = _state(value)
    result = []
    bindings = {config_id: configuration_binding(config_id, configuration, context)
                for config_id, configuration in configurations.items()}
    identities = _assign_identities(bindings, state["entries"])
    for config_id, configuration in configurations.items():
        binding = bindings[config_id]
        ident = identities[config_id]
        entry = state["entries"].get(ident, {})
        profile = configuration["profile"]
        played = max(_timestamp(profile.get("lastUsed")), _timestamp(profile.get("lastPlayed")),
                     _timestamp(entry.get("launchedAt")))
        if not played or entry.get("hidden") or ((legacy_cleared or state.get("hideUntracked")) and not _timestamp(entry.get("launchedAt"))):
            continue
        record = {"id": ident, "source": "Java", "javaInstance": {"configId": config_id}, "epochLastPlayedTimeMs": played}
        custom = entry.get("customization")
        if isinstance(custom, dict):
            record.update({key: custom[key] for key in CUSTOM_FIELDS if key in custom and custom[key] is not None})
        result.append((record, binding))
    result.sort(key=lambda item: ("pinnedIndex" not in item[0], item[0].get("pinnedIndex", 0),
                                 -item[0]["epochLastPlayedTimeMs"], item[0]["id"]))
    pinned = 0
    for record, _ in result:
        if "pinnedIndex" in record:
            record["pinnedIndex"] = pinned
            pinned += 1
    return state, result


def build_slice(configurations, value, context, legacy_cleared=False):
    _, snapshot = _snapshot(configurations, value, context, legacy_cleared)
    records = [record for record, _ in snapshot]
    return {"playRecords": records, "playRecordSourceMap": {source: records if source == "Java" else [] for source in SOURCES}}


def _remember(state, record, binding):
    entry = state["entries"].setdefault(record["id"], {})
    old_sources = entry.get("sources") or []
    primary = entry.get("primarySource") or (old_sources[0] if old_sources else binding.get("primarySource"))
    entry.update({key: copy.deepcopy(binding[key]) for key in ("configId", "context", "profileKey")})
    entry["sources"] = old_sources + [ref for ref in binding["sources"] if ref not in old_sources]
    if primary is not None:
        entry["primarySource"] = primary
    entry.setdefault("customization", {})
    return entry


def _customization(record):
    custom = {key: record[key] for key in CUSTOM_FIELDS if record.get(key) is not None}
    if "name" in custom:
        name = custom["name"]
        if not isinstance(name, str) or len(name) > 256 or any(ord(char) < 32 for char in name):
            raise QuickPlayError("Quick Play names must contain at most 256 printable characters.")
        if not name.strip():
            custom.pop("name")
    if "pinnedIndex" in custom and (type(custom["pinnedIndex"]) is not int or not 0 <= custom["pinnedIndex"] < 5):
        raise QuickPlayError("Pin positions must be integers from 0 to 4.")
    if "letterGlyphVisible" in custom and type(custom["letterGlyphVisible"]) is not bool:
        raise QuickPlayError("Show Title Initial must be a boolean.")
    if "customizedIcon" in custom:
        icon = custom["customizedIcon"]
        if not isinstance(icon, str):
            raise QuickPlayError("Provide a launcher icon or a 128x128 PNG.")
        if icon.startswith("data:image/png;base64,"):
            try:
                if len(icon) > 1024 * 1024:
                    raise ValueError()
                raw = base64.b64decode(icon.split(",", 1)[1], validate=True)
                if raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR" or struct.unpack(">II", raw[16:24]) != (128, 128):
                    raise ValueError()
            except (ValueError, struct.error):
                raise QuickPlayError("Custom Quick Play icons must be 128x128 PNG files.") from None
        elif not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", icon):
            raise QuickPlayError("Provide a valid launcher icon name.")
    return custom


def update(configurations, value, context, incoming, legacy_cleared=False):
    state, snapshot = _snapshot(configurations, value, context, legacy_cleared)
    if not isinstance(incoming, dict) or not isinstance(incoming.get("id"), str):
        raise QuickPlayError("Provide a Quick Play record to edit.")
    chosen = next((pair for pair in snapshot if pair[0]["id"] == incoming["id"]), None)
    if chosen is None:
        raise QuickPlayError("This Quick Play installation is no longer available.", 404, "missing_quickplay")
    record, binding = chosen
    if incoming.get("source", "Java") != "Java" or incoming.get("javaInstance", record["javaInstance"]) != record["javaInstance"]:
        raise QuickPlayError("A Quick Play record cannot be moved to another installation.")
    custom = _customization(incoming)
    pinned = [pair for pair in snapshot if "pinnedIndex" in pair[0] and pair[0]["id"] != record["id"]]
    if "pinnedIndex" in custom:
        if len(pinned) >= 5:
            raise QuickPlayError("The Quick Play bar supports five pinned installations.")
        pinned.insert(min(custom["pinnedIndex"], len(pinned)), chosen)
    entry = _remember(state, record, binding)
    entry["customization"] = custom
    for index, (item, item_binding) in enumerate(pinned):
        _remember(state, item, item_binding)["customization"]["pinnedIndex"] = index
    return state


def delete(configurations, value, context, body, legacy_cleared=False):
    state, snapshot = _snapshot(configurations, value, context, legacy_cleared)
    if body.get("all") is True:
        state["hideUntracked"] = True
        for entry in state["entries"].values():
            entry["hidden"] = True
        chosen = snapshot
    elif isinstance(body.get("id"), str):
        chosen = [pair for pair in snapshot if pair[0]["id"] == body["id"]]
        if not chosen:
            raise QuickPlayError("This Quick Play installation is no longer available.", 404, "missing_quickplay")
    else:
        raise QuickPlayError("Provide a Quick Play record ID or all:true.")
    for record, binding in chosen:
        _remember(state, record, binding)["hidden"] = True
    return state


def record_launch(config_id, configuration, value, context, timestamp):
    state = _state(value)
    binding = configuration_binding(config_id, configuration, context)
    ident = _identity(binding, state["entries"])
    entry = _remember(state, {"id": ident}, binding)
    entry.update(launchedAt=int(timestamp), hidden=False)
    return state
