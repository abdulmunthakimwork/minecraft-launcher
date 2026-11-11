"""Bounded local inventory; files are inspected as data and never executed.

The installation service owns profile/private-root policy and the global launcher
operation coordinator. This module supplies in-process canonical-root locking,
link refusal, compare-and-swap revisions and a durable rollback journal. Backups
are intentionally retained in ``.launcher-content/history`` on the game volume.
"""
import base64
import builtins
import copy
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import download_control
import re
import shutil
import stat
import struct
import threading
import tomllib
import uuid
import zipfile

try:
    from content_provider import ProviderError, optional_icon_url, raster_image_type
except ImportError:
    from .content_provider import ProviderError, optional_icon_url, raster_image_type

KINDS = ("mods", "resourcepacks", "shaderpacks")
FILE_LIMIT = 512 * 1024 * 1024
METADATA_LIMIT = 1024 * 1024
ENTRY_LIMIT = 10000
STATE_LIMIT = 8 * 1024 * 1024
DISABLED = ".disabled"
_PROVIDER_FIELDS = ("provider", "projectId", "versionId", "userRequested", "requiredBy",
    "providerDependencies", "embeddedProjects", "embeddedVersions", "releaseChannels", "gameVersions", "loaders", "iconUrl")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)
# Store the registry outside this reloadable module; the host hot-reloads Python.
if not hasattr(builtins, "_launcher_content_root_locks"):
    builtins._launcher_content_root_locks = ({}, threading.Lock())
_LOCKS, _LOCKS_GUARD = builtins._launcher_content_root_locks


class ContentError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _reparse(path):
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _no_links(path):
    # Inspect lexical components before collapsing '..': link/../safe must not
    # hide a link that the filesystem would traverse in the original spelling.
    lexical = Path(path)
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    current = Path(lexical.anchor)
    for name in lexical.parts[1:]:
        if name == "..":
            current = current.parent
        else:
            _filename(name, allow_disabled=True)
            current = current / name
        if os.path.lexists(current) and _reparse(current):
            raise ContentError("unsafe_path", "Links and reparse points cannot be managed.", {"path": str(current)})
    return Path(os.path.abspath(os.fspath(path)))


def canonical_root(root):
    if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
        raise ContentError("unsafe_path", "An installation directory is required.")
    # Refuse alternate Windows namespace/ADS spellings before resolve normalizes.
    raw = os.fspath(root)
    if os.name == "nt" and (raw.startswith(("\\\\?\\", "\\\\.\\")) or ":" in os.path.splitdrive(raw)[1]):
        raise ContentError("unsafe_path", "Unsupported Windows directory spelling.")
    path = _no_links(root)
    return os.path.normcase(str(path.resolve()))


def _filename(name, allow_disabled=False):
    if (not isinstance(name, str) or not name or name in (".", "..") or len(name) > 255
            or any(ord(c) < 32 or c in '/\\:*?"<>|' for c in name)
            or name.endswith((".", " ")) or _RESERVED.match(name)
            or (not allow_disabled and name.casefold().endswith(DISABLED))):
        raise ContentError("unsafe_path", "The filename is not a safe Windows filename.", {"filename": name})
    return name


def _relative(path):
    if not isinstance(path, str) or "\\" in path:
        raise ContentError("unsafe_path", "A canonical relative content path is required.")
    parts = path.split("/")
    if len(parts) != 2 or parts[0] not in KINDS:
        raise ContentError("unsafe_path", "Content paths must belong to a supported content folder.")
    _filename(parts[1], allow_disabled=True)
    return path


def _key(kind, filename):
    normalized = filename.casefold()
    if normalized.endswith(DISABLED):
        normalized = normalized[:-len(DISABLED)]
    return kind + "/" + normalized


def _ident(kind, filename):
    return hashlib.sha256(_key(kind, filename).encode("utf-8")).hexdigest()[:32]


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _bounded_json(path, limit=STATE_LIMIT):
    _no_links(path)
    with open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ContentError("metadata_limit", "Saved metadata exceeds the allowed size.")
    return json.loads(raw.decode("utf-8-sig"))


def _sync_dir(path):
    if os.name != "nt":
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass


def _write_json(path, value):
    path = _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _encoded(value)
    if len(raw) > STATE_LIMIT:
        raise ContentError("metadata_limit", "Saved metadata exceeds the allowed size.")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with open(temporary, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _no_links(path)
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _file_hash(path):
    _no_links(path)
    if not path.is_file():
        raise ContentError("unsupported_content", "Only regular files may be acquired.")
    if path.stat().st_size > FILE_LIMIT:
        raise ContentError("file_limit", "Content exceeds the 512 MiB file limit.")
    hashes = {kind: hashlib.new(kind) for kind in ("sha1", "sha256", "sha512")}
    count = 0
    with open(path, "rb") as handle:
        while block := handle.read(1024 * 1024):
            count += len(block)
            if count > FILE_LIMIT:
                raise ContentError("file_limit", "Content grew beyond the file limit.")
            for digest in hashes.values():
                digest.update(block)
    return {name: digest.hexdigest() for name, digest in hashes.items()}


def _tree_hash(path):
    _no_links(path)
    if not path.is_dir():
        return _file_hash(path)
    records, count, total = [], 0, 0
    for parent, directories, files in os.walk(path, followlinks=False):
        names = directories + files
        if len({name.casefold() for name in names}) != len(names):
            raise ContentError("unsafe_path", "Unpacked resource entries have Windows case aliases.")
        for name in sorted(directories + files, key=str.casefold):
            count += 1
            if count > ENTRY_LIMIT:
                raise ContentError("file_limit", "The unpacked resource has too many entries.")
            _filename(name, allow_disabled=True)
            child = Path(parent) / name
            _no_links(child)
            relative = child.relative_to(path).as_posix()
            if child.is_file():
                total += child.stat().st_size
                if total > FILE_LIMIT:
                    raise ContentError("file_limit", "Unpacked resource bytes exceed the inspection limit.")
            records.append([relative, "directory"] if child.is_dir() else [relative, _file_hash(child)])
    return {name: hashlib.new(name, _encoded(sorted(records))).hexdigest() for name in ("sha1", "sha256", "sha512")}


def _archive_names(archive):
    entries = archive.infolist()
    if len(entries) > ENTRY_LIMIT:
        raise ContentError("metadata_limit", "The archive has too many entries.")
    seen, prefixes = set(), {}
    file_paths = {entry.filename.casefold() for entry in entries if not entry.is_dir()}
    for entry in entries:
        name = entry.filename.rstrip("/")
        if not name or "\\" in name or name.startswith("/"):
            raise ContentError("unsafe_archive", "Unsafe archive entry.", {"entry": entry.filename})
        try:
            for part in name.split("/"):
                _filename(part, allow_disabled=True)
        except ContentError as exc:
            raise ContentError("unsafe_archive", "Unsafe archive entry.", {"entry": entry.filename}) from exc
        if name.casefold() in seen or stat.S_ISLNK((entry.external_attr >> 16) & 0xFFFF):
            raise ContentError("unsafe_archive", "Archive entries contain links or Windows path aliases.")
        seen.add(name.casefold())
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            key = prefix.casefold()
            if key in prefixes and prefixes[key] != prefix:
                raise ContentError("unsafe_archive", "Archive parent directories contain Windows case aliases.")
            prefixes[key] = prefix
            if index < len(parts) and key in file_paths:
                raise ContentError("unsafe_archive", "An archive path is both a file and a directory.")
    return {entry.filename: entry for entry in entries}


@contextmanager
def _zip_archive(source):
    """Bound central-directory allocations before ZipFile constructs entries."""
    handle = source if hasattr(source, "read") else open(source, "rb")
    try:
        handle.seek(0, os.SEEK_END)
        length = handle.tell()
        handle.seek(max(0, length - 65557))
        tail = handle.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise zipfile.BadZipFile("Missing archive directory")
        fields = struct.unpack_from("<4s4H2LH", tail, offset)
        count, size = fields[4], fields[5]
        absolute = max(0, length - 65557) + offset
        directory_end = absolute
        if fields[1] or fields[2]:
            raise ContentError("unsafe_archive", "Multipart archives cannot be inspected.")
        if count == 65535 or size == 0xFFFFFFFF:
            handle.seek(absolute - 20)
            locator = handle.read(20)
            if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
                raise zipfile.BadZipFile("Missing ZIP64 locator")
            position = struct.unpack("<4sLQL", locator)[2]
            directory_end = position
            handle.seek(position)
            record = handle.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise zipfile.BadZipFile("Invalid ZIP64 directory")
            values = struct.unpack("<4sQ2H2L4Q", record)
            count, size = values[7], values[8]
        if count > ENTRY_LIMIT or size > 32 * METADATA_LIMIT:
            raise ContentError("metadata_limit", "The archive directory exceeds the inspection limit.")
        # EOCD counters are attacker-controlled. Count fixed-size directory
        # records directly, without allocating ZipInfo objects or entry names.
        start = directory_end - size
        if start < 0:
            raise zipfile.BadZipFile("Invalid archive directory offset")
        handle.seek(start)
        consumed, actual_count = 0, 0
        while consumed < size:
            header = handle.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise zipfile.BadZipFile("Invalid archive directory record")
            actual_count += 1
            if actual_count > ENTRY_LIMIT:
                raise ContentError("metadata_limit", "The archive has too many directory entries.")
            lengths = struct.unpack_from("<3H", header, 28)
            consumed += 46 + sum(lengths)
            if consumed > size:
                raise zipfile.BadZipFile("Archive directory record exceeds its bounds")
            handle.seek(sum(lengths), os.SEEK_CUR)
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            yield archive
    finally:
        handle.close()


def _member(archive, names, name, limit=METADATA_LIMIT):
    if name not in names:
        return None
    info = names[name]
    if info.file_size > limit:
        raise ContentError("metadata_limit", "Archive metadata exceeds the 1 MiB inspection limit.", {"entry": name})
    with archive.open(info) as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ContentError("metadata_limit", "Archive metadata exceeds the inspection limit.")
    return raw


def _icon_data(raw):
    if raw is None or len(raw) > 256 * 1024:
        return None
    try:
        content_type = raster_image_type(raw)
    except ProviderError:
        return None
    return 'data:' + content_type + ';base64,' + base64.b64encode(raw).decode('ascii')


def _archive_icon(archive, names, value, result):
    if result.get('iconData'):
        return
    if isinstance(value, dict):
        choices = [(int(size), path) for size, path in value.items()
                   if isinstance(size, str) and re.fullmatch(r'[0-9]{1,4}', size) and isinstance(path, str)]
        choices.sort(key=lambda entry: (entry[0] < 64, entry[0] if entry[0] >= 64 else -entry[0]))
        value = choices[0][1] if choices else None
    if (not isinstance(value, str) or not value or len(value) > 512 or '\\' in value or
            any(part in ('', '.', '..') for part in value.split('/'))):
        return
    try:
        raw = _member(archive, names, value, limit=256 * 1024)
        data = _icon_data(raw)
        if data:
            result['iconData'] = data
    except (ContentError, OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        pass  # Optional artwork must not hide valid loader metadata.


def _relations(value, quilt=False):
    output = []
    if quilt and isinstance(value, dict) and "id" in value:
        value = [value]
    if isinstance(value, dict):
        for ident, requirement in value.items():
            output.append({"id": ident, "requirement": requirement})
    elif isinstance(value, list):
        for relation in value:
            if isinstance(relation, str):
                output.append({"id": relation, "requirement": "*"})
            elif isinstance(relation, dict):
                if isinstance(relation.get("id"), str):
                    item = {"id": relation["id"], "requirement": relation.get("versions", relation.get("version", "*"))}
                    for name in ("optional", "unless"):
                        if name in relation:
                            item[name] = relation[name]
                    if "unless" in relation:
                        item["unsupported"] = True
                    output.append(item)
                else:
                    output.append({"id": None, "requirement": relation, "unsupported": True})
            else:
                output.append({"id": None, "requirement": relation, "unsupported": True})
    return output


def _display_metadata(name, version):
    if name is not None and not isinstance(name, str):
        raise ValueError("Content names must be strings")
    if version is not None and not isinstance(version, (str, int, float)):
        raise ValueError("Content versions must be strings")
    return name, str(version) if version is not None else None


def _append_mod(result, ident, version=None, depth=0):
    if isinstance(ident, str) and ident:
        if ident not in result["modIds"]:
            result["modIds"].append(ident)
        classification = "embeddedModIds" if depth else "rootModIds"
        if ident not in result.setdefault(classification, []):
            result[classification].append(ident)
        if isinstance(version, (str, int, float)):
            result.setdefault("providedVersions", {})[ident] = str(version)


def _manifest_version(archive, names, budget):
    raw = _member(archive, names, "META-INF/MANIFEST.MF")
    if raw is None:
        return None
    budget["metadata"] += len(raw)
    if budget["metadata"] > 8 * METADATA_LIMIT:
        raise ContentError("metadata_limit", "Combined archive metadata exceeds the inspection limit.")
    fields = []
    for line in raw.decode("utf-8-sig").splitlines():
        if not line:
            break  # Named-entry sections do not supply the archive version.
        if line.startswith(" ") and fields:
            fields[-1] += line[1:]
        else:
            fields.append(line)
    for field in fields:
        name, separator, value = field.partition(":")
        if separator and name.casefold() == "implementation-version":
            return value.strip() or None
    return None


def _parse_archive(archive, result, depth=0, budget=None, location=(), parents=()):
    budget = budget if budget is not None else {"metadata": 0, "embedded": 0, "archives": 0}
    budget["archives"] += 1
    if budget["archives"] > 256:
        raise ContentError("metadata_limit", "Embedded archive inspection is limited to 256 archives.")
    if depth > 4:
        raise ContentError("metadata_limit", "Embedded archive inspection is limited to four levels.")
    names = _archive_names(archive)
    formats = {"fabric.mod.json": "fabric", "quilt.mod.json": "quilt",
        "META-INF/neoforge.mods.toml": "neoforge", "META-INF/mods.toml": "forge"}
    present_formats = {descriptor: format for descriptor, format in formats.items() if descriptor in names}
    if depth == 0 and present_formats:
        result["rootMetadataFormats"] = []
        result["rootModIds"] = []
    manifest_version = NotImplemented
    embedded = []
    for descriptor in ("fabric.mod.json", "quilt.mod.json", "META-INF/neoforge.mods.toml", "META-INF/mods.toml", "mcmod.info", "pack.mcmeta"):
        raw = _member(archive, names, descriptor)
        if raw is None:
            continue
        budget["metadata"] += len(raw)
        if budget["metadata"] > 8 * METADATA_LIMIT:
            raise ContentError("metadata_limit", "Combined archive metadata exceeds the inspection limit.")
        try:
            text = raw.decode("utf-8-sig")
            # The loader's Gson reader accepts literal newlines in strings.
            # Keep strict JSON for non-loader documents and bounded archive reads.
            data = tomllib.loads(text) if descriptor.endswith(".toml") else json.loads(
                text, strict=descriptor not in ("fabric.mod.json", "quilt.mod.json"))
        except (ValueError, UnicodeError) as exc:
            result.setdefault("metadataProblems", []).append({"code": "unknown_metadata",
                "sourceFormat": present_formats.get(descriptor), "entry": descriptor,
                "message": "This loader descriptor could not be inspected.", "details": str(exc)[:300]})
            continue
        relation_lengths = {field: len(result.get(field, [])) for field in ("depends", "conflicts", "optionalDepends", "warnings")}
        candidate_versions, candidate_ident, candidate_environment, declared = {}, None, None, []
        if descriptor == "fabric.mod.json":
            if not isinstance(data, dict):
                raise ValueError("Fabric metadata must be an object")
            _display_metadata(data.get("name"), data.get("version"))
            _append_mod(result, data.get("id"), data.get("version"), depth)
            candidate_ident, candidate_environment = data.get("id"), data.get("environment")
            if isinstance(candidate_ident, str) and candidate_ident:
                candidate_versions[candidate_ident] = str(data["version"]) if data.get("version") is not None else None
            for alias in data.get("provides", []):
                _append_mod(result, alias, data.get("version"), depth)
                if isinstance(alias, str) and alias:
                    candidate_versions[alias] = candidate_versions.get(candidate_ident)
            result["depends"].extend(_relations(data.get("depends", {})))
            result["conflicts"].extend(_relations(data.get("breaks", {})))
            result.setdefault("optionalDepends", []).extend(_relations(data.get("recommends", {})) + _relations(data.get("suggests", {})))
            result.setdefault("warnings", []).extend(_relations(data.get("conflicts", {})))
            for child in data.get("jars", []):
                if isinstance(child, dict) and isinstance(child.get("file"), str):
                    declared.append(child["file"])
            for field in ("name", "version", "environment"):
                if field in data and depth == 0:
                    result[field] = str(data[field]) if field == "version" and data[field] is not None else data[field]
            if depth == 0:
                _archive_icon(archive, names, data.get('icon'), result)
            result.setdefault("metadataFormats", []).append("fabric")
        elif descriptor == "quilt.mod.json":
            loader = data.get("quilt_loader", {})
            name, version = _display_metadata(loader.get("metadata", {}).get("name", result["name"]), loader.get("version"))
            _append_mod(result, loader.get("id"), loader.get("version"), depth)
            candidate_ident = loader.get("id")
            if isinstance(candidate_ident, str) and candidate_ident:
                candidate_versions[candidate_ident] = str(loader["version"]) if loader.get("version") is not None else None
            for alias in loader.get("provides", []):
                _append_mod(result, alias.get("id") if isinstance(alias, dict) else alias,
                    alias.get("version", loader.get("version")) if isinstance(alias, dict) else loader.get("version"), depth)
                alias_id = alias.get("id") if isinstance(alias, dict) else alias
                alias_version = alias.get("version", loader.get("version")) if isinstance(alias, dict) else loader.get("version")
                if isinstance(alias_id, str) and alias_id:
                    candidate_versions[alias_id] = str(alias_version) if alias_version is not None else None
            for relation in _relations(loader.get("depends", []), quilt=True):
                result.setdefault("optionalDepends" if relation.get("optional") is True else "depends", []).append(relation)
            result["conflicts"].extend(_relations(loader.get("breaks", []), quilt=True))
            if depth == 0:
                result.update(name=name, version=version)
                _archive_icon(archive, names, loader.get('metadata', {}).get('icon'), result)
            for child in loader.get("jars", []):
                declared.append(child if isinstance(child, str) else child.get("file"))
            result.setdefault("metadataFormats", []).append("quilt")
        elif descriptor.endswith(".toml"):
            if "loaderVersion" in data:
                loader = data.get("modLoader")
                result["depends"].append({"id": loader if isinstance(loader, str) else None,
                    "requirement": data["loaderVersion"], **({"unsupported": True} if not isinstance(loader, str) else {})})
            for index, mod in enumerate(data.get("mods", [])):
                name, version = _display_metadata(mod.get("displayName", result["name"]), mod.get("version"))
                if version and "${file.jarVersion}" in version:
                    if manifest_version is NotImplemented:
                        manifest_version = _manifest_version(archive, names, budget)
                    version = version.replace("${file.jarVersion}", manifest_version) if manifest_version else None
                    if version is None:
                        result.setdefault("metadataProblems", []).append({"code": "unresolved_version", "message": "The archive manifest does not declare the version required by its mod metadata."})
                _append_mod(result, mod.get("modId"), version, depth)
                if index == 0 and depth == 0:
                    result.update(name=name, version=version)
                    _archive_icon(archive, names, mod.get('logoFile'), result)
            for owner, relations in data.get("dependencies", {}).items():
                for relation in relations:
                    item = {"id": relation.get("modId"), "requirement": relation.get("versionRange", "*"), "owner": owner}
                    for field in ("side", "ordering"):
                        if field in relation:
                            item[field] = relation[field]
                    relation_type = relation.get("type", "required" if relation.get("mandatory", True) else "optional")
                    destination = "warnings" if relation_type == "discouraged" else "conflicts" if relation_type == "incompatible" else "depends" if relation_type == "required" else "optionalDepends"
                    result.setdefault(destination, []).append(item)
            result.setdefault("metadataFormats", []).append("neoforge" if "neoforge" in descriptor else "forge")
        elif descriptor == "mcmod.info":
            mods = data if isinstance(data, list) else data.get("modList", [])
            for index, mod in enumerate(mods):
                name, version = _display_metadata(mod.get("name", result["name"]), mod.get("version"))
                _append_mod(result, mod.get("modid"), mod.get("version"), depth)
                if index == 0 and depth == 0:
                    result.update(name=name, version=version)
                    _archive_icon(archive, names, mod.get('logoFile'), result)
                for relation in mod.get("requiredMods", mod.get("dependencies", [])):
                    if isinstance(relation, str):
                        ident, separator, version = relation.partition("@")
                        result["depends"].append({"id": ident, "requirement": version if separator else "*"})
            result.setdefault("metadataFormats", []).append("legacy")
        elif descriptor == "pack.mcmeta":
            _pack_metadata(data, result, display=depth == 0 and result.get("kind") != "mods")
            if depth == 0 and result.get('kind') != 'mods':
                _archive_icon(archive, names, 'pack.png', result)
        if depth == 0 and descriptor in present_formats and present_formats[descriptor] not in result["rootMetadataFormats"]:
            result["rootMetadataFormats"].append(present_formats[descriptor])
        if len(present_formats) > 1 and descriptor in present_formats:
            for field, start in relation_lengths.items():
                for relation in result.get(field, [])[start:]:
                    relation["sourceFormat"] = present_formats[descriptor]
        candidate_key = None
        if candidate_versions and descriptor in ("fabric.mod.json", "quilt.mod.json"):
            source_format = present_formats[descriptor]
            candidate_key = hashlib.sha256(_encoded([list(location), source_format, candidate_ident])).hexdigest()
            candidate = {"key": candidate_key, "modId": candidate_ident,
                "modIds": list(candidate_versions), "providedVersions": candidate_versions,
                "version": candidate_versions[candidate_ident], "sourceFormat": source_format,
                "root": depth == 0, "parents": list(parents), "environment": candidate_environment}
            for field, start in relation_lengths.items():
                candidate[field] = copy.deepcopy(result.get(field, [])[start:])
                for relation in candidate[field]:
                    relation["sourceFormat"] = source_format
            candidates = result.setdefault("modCandidates", [])
            previous = next((entry for entry in candidates if entry["key"] == candidate_key), None)
            if previous:
                previous["parents"] = sorted(set(previous["parents"]) | set(parents))
            else:
                if len(candidates) >= 256:
                    raise ContentError("metadata_limit", "Embedded module candidates exceed the inspection limit.")
                candidates.append(candidate)
        embedded.extend((name, candidate_key) for name in declared)
    for name, parent_key in embedded:
        if not isinstance(name, str):
            continue
        raw = _member(archive, names, name, limit=8 * METADATA_LIMIT)
        if raw is None:
            result.setdefault("metadataProblems", []).append({"code": "missing_embedded", "entry": name})
        else:
            budget["embedded"] += len(raw)
            if budget["embedded"] > 32 * METADATA_LIMIT:
                raise ContentError("metadata_limit", "Embedded archive bytes exceed the inspection limit.")
            with _zip_archive(io.BytesIO(raw)) as child:
                _parse_archive(child, result, depth + 1, budget, location + (name,),
                    (parent_key,) if parent_key else parents)


def _pack_metadata(data, result, display=True):
    pack = data.get("pack", {})
    result["packFormat"] = pack.get("pack_format")
    if display and isinstance(pack.get("description"), str):
        result["name"] = pack["description"]
    elif display and "description" in pack:
        result["description"] = pack["description"]
    result.setdefault("metadataFormats", []).append("pack")


def _inspect(path, kind, filename):
    item = {"id": _ident(kind, filename), "kind": kind, "path": kind + "/" + filename,
        "filename": filename, "name": filename, "version": None,
        "enabled": not filename.casefold().endswith(DISABLED), "hashes": {}, "modIds": [], "depends": [], "conflicts": []}
    problems = []
    try:
        _filename(filename, allow_disabled=True)
        item["hashes"] = _tree_hash(path)
        if path.is_dir():
            item["directory"] = True
            descriptor = path / "pack.mcmeta"
            if descriptor.exists():
                _pack_metadata(_bounded_json(descriptor, METADATA_LIMIT), item)
                icon = path / 'pack.png'
                try:
                    _no_links(icon)
                    if icon.is_file() and icon.stat().st_size <= 256 * 1024:
                        with open(icon, 'rb') as handle:
                            data = _icon_data(handle.read(256 * 1024 + 1))
                        if data:
                            item['iconData'] = data
                except (ContentError, OSError):
                    pass
        elif filename.casefold().removesuffix(DISABLED).endswith((".jar", ".zip")):
            with _zip_archive(path) as archive:
                _parse_archive(archive, item)
    except ContentError as exc:
        problems.append({"code": exc.code, "message": str(exc), "details": exc.details})
    except (OSError, ValueError, TypeError, AttributeError, KeyError, zipfile.BadZipFile, RuntimeError, RecursionError) as exc:
        problems.append({"code": "unknown_metadata", "message": "The content metadata could not be inspected.", "details": str(exc)[:300]})
    if not item.get("metadataFormats"):
        item["unknown"] = True
        if not problems:
            problems.append({"code": "unknown_metadata", "message": "This file has no supported local metadata."})
    item.setdefault("metadataProblems", []).extend(problems)
    for problem in item["metadataProblems"]:
        problem.setdefault("path", item["path"])
    return item


def inspect_file(source, kind="mods", filename=None):
    """Inspect a verified local stage for resolver validation without mutation.

    Structural identity always comes from the bytes. Unknown metadata remains
    explicit; path/archive/size violations reject the proposed acquisition.
    """
    if kind not in KINDS:
        raise ContentError("invalid_change", "Unsupported content kind.")
    source = _no_links(source)
    if not source.is_file():
        raise ContentError("invalid_change", "A staged regular local file is required.")
    filename = _filename(filename or source.name)
    item = _inspect(source, kind, filename)
    for problem in item.get("metadataProblems", []):
        if problem["code"] in ("unsafe_path", "unsafe_archive", "metadata_limit", "file_limit"):
            raise ContentError(problem["code"], problem["message"], problem.get("details"))
    if not item["hashes"]:
        raise ContentError("unsupported_content", "The acquired file cannot be fingerprinted safely.")
    return item


class ContentStore:
    def __init__(self, root, data_dir, instance_id, is_running=None, can_recover=None, allow_missing=False):
        self.root = Path(canonical_root(root))
        self.data_dir = Path(canonical_root(data_dir))
        self.allow_missing = allow_missing
        if not self.root.is_dir() and (not allow_missing or os.path.lexists(self.root)):
            raise ContentError("missing_root", "The installation directory does not exist.")
        self.instance_id = str(instance_id)
        self.is_running = is_running or (lambda: False)
        self.can_recover = can_recover or (lambda: True)
        root_id = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
        self.state_dir = self.data_dir / "instance-content" / root_id
        self.manifest_path = self.state_dir / "manifest.json"
        self.private_dir = self.root / ".launcher-content"
        self.journal_path = self.private_dir / "journal.json"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.root), threading.RLock())

    def _root_guard(self):
        missing = self.allow_missing and not os.path.lexists(self.root)
        if canonical_root(self.root) != str(self.root) or not (self.root.is_dir() or missing):
            raise ContentError("unsafe_path", "The installation directory changed.")
        _no_links(self.state_dir)
        _no_links(self.private_dir)

    def _path(self, relative):
        return _no_links(self.root / _relative(relative))

    def _manifest(self):
        if not self.manifest_path.exists():
            return {"schema": 1, "root": str(self.root), "items": {}}
        try:
            data = _bounded_json(self.manifest_path)
            if data.get("schema") != 1 or data.get("root") != str(self.root) or not isinstance(data.get("items"), dict):
                raise ValueError("Invalid manifest identity")
            return data
        except (ValueError, AttributeError, OSError) as exc:
            raise ContentError("manifest_invalid", "Saved content metadata cannot be read safely.") from exc

    def _scan(self):
        self._root_guard()
        manifest = self._manifest()
        items, problems, reconciled, seen = [], [], {}, set()
        for kind in KINDS:
            folder = self.root / kind
            if not os.path.lexists(folder):
                continue
            try:
                _no_links(folder)
                if not folder.is_dir():
                    raise ContentError("unsafe_path", "The content folder is not a directory.")
                paths = sorted(folder.iterdir(), key=lambda path: (path.name.casefold(), path.name))
                if len(paths) > ENTRY_LIMIT:
                    raise ContentError("file_limit", "The content folder has too many entries.")
            except (ContentError, OSError) as exc:
                problems.append({"code": getattr(exc, "code", "unreadable_content"), "path": kind, "message": str(exc)})
                continue
            for path in paths:
                item = _inspect(path, kind, path.name)
                key = _key(kind, path.name)
                if key in seen:
                    problems.append({"code": "path_alias", "path": item["path"], "message": "Existing files share a Windows/disabled path identity."})
                seen.add(key)
                saved = manifest["items"].get(item["id"])
                if isinstance(saved, dict):
                    if saved.get("hashes") == item["hashes"] and item["hashes"]:
                        metadata = saved.get("metadata", {})
                        if isinstance(metadata, dict):
                            for field in _PROVIDER_FIELDS + ("localModified",):
                                if field in metadata:
                                    if field == 'iconUrl' and optional_icon_url(metadata[field]) is None:
                                        continue
                                    item[field] = copy.deepcopy(metadata[field])
                    else:
                        item["localModified"] = True
                    metadata = {field: item[field] for field in _PROVIDER_FIELDS + ("localModified",) if field in item}
                    reconciled[item["id"]] = {"hashes": item["hashes"], "metadata": metadata, "path": item["path"]}
                if item.get("localModified"):
                    problems.append({"code": "local_changed", "path": item["path"], "message": "The file differs from its saved provider metadata."})
                items.append(item)
                problems.extend(item.get("metadataProblems", []))
        # Reconciliation writes metadata only; arbitrary installation files stay intact.
        new_manifest = {**manifest, "items": reconciled}
        if new_manifest != manifest and self.root.is_dir():
            _write_json(self.manifest_path, new_manifest)
        self._identity_problems(items, problems)
        revision = hashlib.sha256(_encoded({"items": items, "problems": problems, "manifest": new_manifest})).hexdigest()
        return {"revision": revision, "items": items, "problems": problems}

    @staticmethod
    def _identity_problems(items, problems):
        seen = {}
        for item in items:
            if not item["enabled"]:
                continue
            # Fabric/Quilt select a shared bundled library once. Repeated
            # jar-in-jar candidates are not duplicate root installations.
            for ident in item.get("rootModIds", item["modIds"]):
                if ident in seen:
                    problems.append({"code": "duplicate_mod_id", "id": ident, "paths": [seen[ident], item["path"]], "message": "Enabled files provide the same mod ID."})
                seen[ident] = item["path"]

    def inventory(self):
        with self._lock:
            self._root_guard()
            self._recover()
            return self._scan()

    def _check_running(self):
        if self.is_running():
            raise ContentError("game_running", "Stop the game before changing installation content.")

    @staticmethod
    def _metadata(value, hashes):
        if not isinstance(value, dict) or len(_encoded(value)) > METADATA_LIMIT:
            raise ContentError("invalid_change", "Install metadata must be a bounded object.")
        claimed = value.get("hashes", {})
        if not isinstance(claimed, dict):
            raise ContentError("hash_mismatch", "Expected content hashes must be an object.")
        for algorithm, digest in claimed.items():
            if algorithm not in hashes or not isinstance(digest, str) or hashes[algorithm] != digest.lower():
                raise ContentError("hash_mismatch", "The staged content does not match its expected hash.", {"algorithm": algorithm})
        metadata = {}
        for field in ("provider", "projectId", "versionId"):
            if field in value:
                if not isinstance(value[field], str) or not value[field] or len(value[field]) > 512:
                    raise ContentError("invalid_change", "Provider identity must be a bounded string.", {"field": field})
                metadata[field] = value[field]
        icon_url = optional_icon_url(value.get('iconUrl'))
        if icon_url:
            metadata['iconUrl'] = icon_url
        if "userRequested" in value:
            if not isinstance(value["userRequested"], bool):
                raise ContentError("invalid_change", "User ownership must be a boolean.")
            metadata["userRequested"] = value["userRequested"]
        if "requiredBy" in value:
            if not isinstance(value["requiredBy"], list) or len(value["requiredBy"]) > 256 or any(not isinstance(v, str) or len(v) > 512 for v in value["requiredBy"]):
                raise ContentError("invalid_change", "Reverse dependents must be a bounded string list.")
            metadata["requiredBy"] = list(dict.fromkeys(value["requiredBy"]))
        for field, maximum in (("embeddedProjects", 256), ("embeddedVersions", 256), ("gameVersions", 4096), ("loaders", 64)):
            if field in value:
                entries = value[field]
                if (not isinstance(entries, list) or len(entries) > maximum or
                        any(not isinstance(entry, str) or not entry or len(entry) > 512 for entry in entries)):
                    raise ContentError("invalid_change", "Provider compatibility values must be bounded string lists.", {"field": field})
                metadata[field] = list(dict.fromkeys(entries))
        if "releaseChannels" in value:
            channels = value["releaseChannels"]
            if (not isinstance(channels, list) or not 1 <= len(channels) <= 3 or
                    any(channel not in ("release", "beta", "alpha") for channel in channels)):
                raise ContentError("invalid_change", "Release channels must be a nonempty supported list.")
            metadata["releaseChannels"] = list(dict.fromkeys(channels))
        if "providerDependencies" in value:
            relations = value["providerDependencies"]
            if (not isinstance(relations, list) or len(relations) > 256 or
                    any(not isinstance(relation, dict) for relation in relations)):
                raise ContentError("invalid_change", "Provider dependencies must be a bounded relation list.")
            for relation in relations:
                for field in ("project_id", "version_id", "dependency_type"):
                    entry = relation.get(field)
                    if entry is not None and (not isinstance(entry, str) or not entry or len(entry) > 512):
                        raise ContentError("invalid_change", "Provider dependency identities must be bounded strings.")
            metadata["providerDependencies"] = copy.deepcopy(relations)
        return metadata

    @staticmethod
    def _validate_set(items):
        paths, projects, hashes, mod_ids = {}, {}, {}, {}
        for item in items:
            path_key = _key(item["kind"], item["filename"])
            if path_key in paths:
                raise ContentError("path_conflict", "Files share a Windows or disabled path identity.", {"paths": [paths[path_key], item["path"]]})
            paths[path_key] = item["path"]
            if not item["enabled"]:
                continue
            project = (item.get("provider", "local"), item.get("projectId"))
            if project[1]:
                if project in projects:
                    raise ContentError("duplicate_project", "The provider project is already enabled.", {"paths": [projects[project], item["path"]]})
                projects[project] = item["path"]
            digest = item["hashes"].get("sha256")
            if digest:
                if digest in hashes:
                    raise ContentError("duplicate_hash", "The same content is already enabled.", {"paths": [hashes[digest], item["path"]]})
                hashes[digest] = item["path"]
            for ident in item.get("rootModIds", item["modIds"]):
                if ident in mod_ids:
                    raise ContentError("duplicate_mod_id", "Enabled files provide the same mod ID.", {"id": ident, "paths": [mod_ids[ident], item["path"]]})
                mod_ids[ident] = item["path"]

    @staticmethod
    def _editable(item):
        if item.get("localModified"):
            raise ContentError("local_changed", "The file was locally modified; it cannot be silently replaced or removed.", {"id": item["id"], "path": item["path"]})
        if not item["hashes"]:
            raise ContentError("unsupported_content", "Content without a safe verified fingerprint cannot be changed.", {"path": item["path"]})

    def _proposal(self, changes, baseline, manifest):
        working = {item["id"]: copy.deepcopy(item) for item in baseline["items"]}
        metadata = copy.deepcopy(manifest["items"])
        sources = {}
        if not isinstance(changes, list) or len(changes) > 256:
            raise ContentError("invalid_change", "A transaction requires at most 256 changes.")
        for change in changes:
            if not isinstance(change, dict):
                raise ContentError("invalid_change", "Each change must be an object.")
            action = change.get("action")
            if action in ("remove", "toggle"):
                ident = change.get("id")
                if not isinstance(ident, str) or ident not in working:
                    raise ContentError("unknown_item", "The selected content item no longer exists.", {"id": ident})
                item = working[ident]
                self._editable(item)
                if action == "remove":
                    working.pop(ident)
                    metadata.pop(ident, None)
                    sources.pop(ident, None)
                    continue
                enabled = change.get("enabled")
                if not isinstance(enabled, bool):
                    raise ContentError("invalid_change", "Enabled state must be a boolean.")
                if item["enabled"] == enabled:
                    continue
                source = sources.get(ident, self._path(item["path"]))
                name = item["filename"][:-len(DISABLED)] if enabled else item["filename"] + DISABLED
                _filename(name, allow_disabled=True)
                item.update(enabled=enabled, filename=name, path=item["kind"] + "/" + name)
                sources[ident] = source
                if ident in metadata:
                    metadata[ident]["path"] = item["path"]
                continue
            if action != "install":
                raise ContentError("invalid_change", "Unsupported content transaction action.")
            kind, filename = change.get("kind"), change.get("filename")
            if kind not in KINDS:
                raise ContentError("invalid_change", "Unsupported content kind.")
            _filename(filename)
            self._path(kind + "/" + filename)
            source_arg = change.get("source")
            if not isinstance(source_arg, (str, os.PathLike)) or not os.fspath(source_arg):
                raise ContentError("invalid_change", "A staged local file is required.")
            source = _no_links(source_arg)
            if not source.is_file():
                raise ContentError("invalid_change", "The acquired local file does not exist.")
            candidate = inspect_file(source, kind, filename)
            saved_metadata = self._metadata(change.get("metadata", {}), candidate["hashes"])
            candidate.update(saved_metadata)
            replacements = change.get("replaces", [])
            if isinstance(replacements, str):
                replacements = [replacements]
            if not isinstance(replacements, list) or any(not isinstance(ident, str) for ident in replacements):
                raise ContentError("invalid_change", "Replacement identities must be installed item IDs.")
            # Same-byte retries reuse the item, even if the catalog renamed its file.
            matching = next((item for item in working.values() if item["kind"] == kind and item["hashes"] == candidate["hashes"]), None)
            if matching and (not replacements or replacements == [matching["id"]]):
                if not matching.get("localModified"):
                    old_metadata = metadata.get(matching["id"], {}).get("metadata", {})
                    combined = {**saved_metadata, **old_metadata}
                    for field in _PROVIDER_FIELDS[5:]:
                        if field in saved_metadata:
                            combined[field] = copy.deepcopy(saved_metadata[field])
                    if "userRequested" in saved_metadata:
                        combined["userRequested"] = bool(old_metadata.get("userRequested") or saved_metadata["userRequested"])
                    if "requiredBy" in saved_metadata:
                        combined["requiredBy"] = saved_metadata["requiredBy"]
                    if combined:
                        metadata[matching["id"]] = {"path": matching["path"], "hashes": matching["hashes"], "metadata": combined}
                        matching.update(combined)
                continue
            for ident in replacements:
                if ident not in working:
                    raise ContentError("unknown_item", "The replaced item no longer exists.", {"id": ident})
                self._editable(working[ident])
                if candidate["id"] == ident and working[ident]["path"] != candidate["path"]:
                    raise ContentError("path_conflict", "A case-only filename replacement aliases the existing file.")
                working.pop(ident)
                metadata.pop(ident, None)
                sources.pop(ident, None)
            if candidate["id"] in working:
                raise ContentError("path_conflict", "An existing filename requires an explicit verified replacement.", {"path": candidate["path"]})
            working[candidate["id"]] = candidate
            sources[candidate["id"]] = source
            metadata[candidate["id"]] = {"path": candidate["path"], "hashes": candidate["hashes"], "metadata": saved_metadata}
        self._validate_set(working.values())
        return list(working.values()), sources, {**manifest, "items": metadata}

    @staticmethod
    def _copy_verified(source, destination, expected):
        source, destination = _no_links(source), _no_links(destination)
        if _tree_hash(source) != expected:
            raise ContentError("local_changed", "Content changed while it was being staged.")
        if source.is_dir():
            shutil.copytree(source, destination, copy_function=shutil.copyfile)
            files = [Path(parent) / name for parent, _, names in os.walk(destination) for name in names]
        else:
            shutil.copyfile(source, destination)
            files = [destination]
        for path in files:
            with open(path, "r+b") as handle:
                os.fsync(handle.fileno())
        if _tree_hash(destination) != expected or _tree_hash(source) != expected:
            raise ContentError("hash_mismatch", "Staging did not preserve the verified content.")
        _sync_dir(destination.parent)

    def _history_path(self, relative, transaction):
        parts = relative.split("/") if isinstance(relative, str) else []
        if len(parts) != 3 or parts[:2] != ["history", transaction] or not re.fullmatch(r"(?:before|after|restore)-\d+", parts[2]):
            raise ContentError("journal_invalid", "Journal artifact paths are invalid.")
        return _no_links(self.private_dir.joinpath(*parts))

    @staticmethod
    def _hashes_valid(hashes):
        return isinstance(hashes, dict) and all(isinstance(hashes.get(name), str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", hashes[name]) for name, length in (("sha1", 40), ("sha256", 64), ("sha512", 128)))

    def _validate_journal(self, journal):
        if not isinstance(journal, dict) or journal.get("schema") != 1 or journal.get("root") != str(self.root):
            raise ContentError("journal_invalid", "The transaction journal does not belong to this installation.")
        transaction = journal.get("transaction")
        if not isinstance(transaction, str) or not re.fullmatch("[0-9a-f]{32}", transaction):
            raise ContentError("journal_invalid", "Invalid transaction identity.")
        for field in ("beforeManifest", "afterManifest"):
            value = journal.get(field)
            if not isinstance(value, dict) or value.get("schema") != 1 or value.get("root") != str(self.root) or not isinstance(value.get("items"), dict):
                raise ContentError("journal_invalid", "Invalid journal manifest.")
        operations = journal.get("operations")
        if not isinstance(operations, list) or len(operations) > 512:
            raise ContentError("journal_invalid", "Invalid journal operations.")
        seen = set()
        for op in operations:
            if not isinstance(op, dict):
                raise ContentError("journal_invalid", "Invalid journal operation.")
            path = _relative(op.get("path"))
            if path.casefold() in seen:
                raise ContentError("journal_invalid", "Repeated journal target.")
            seen.add(path.casefold())
            for field in ("before", "after"):
                artifact = op.get(field)
                if artifact is not None:
                    if not isinstance(artifact, dict) or not self._hashes_valid(artifact.get("hashes")):
                        raise ContentError("journal_invalid", "Invalid journal fingerprint.")
                    self._history_path(artifact.get("artifact"), transaction)
        return journal

    def _current_hash(self, path):
        self._root_guard()
        _no_links(path)
        return _tree_hash(path) if os.path.lexists(path) else None

    def _delete(self, path):
        path = _no_links(path)
        # Every live deletion is an already validated content-relative target.
        _relative(path.relative_to(self.root).as_posix())
        if path.is_dir():
            _tree_hash(path)  # Refuse links anywhere in an unpacked resource.
            shutil.rmtree(path)
        elif os.path.lexists(path):
            path.unlink()
        _sync_dir(path.parent)

    def _finish_journal(self, journal, status):
        result = {"transaction": journal["transaction"], "status": status}
        _write_json(self.private_dir / "history" / journal["transaction"] / "result.json", result)
        _no_links(self.journal_path)
        self.journal_path.unlink()
        _sync_dir(self.private_dir)

    def _rollback(self, journal):
        self._check_running()
        current_manifest = self._manifest()
        if current_manifest not in (journal["beforeManifest"], journal["afterManifest"]):
            raise ContentError("recovery_conflict", "Saved metadata changed after the interrupted transaction.")
        # Validate every target and backup before restoring even the first file.
        for op in journal["operations"]:
            path = self._path(op["path"])
            before, after = op["before"], op["after"]
            current = self._current_hash(path)
            allowed = [value["hashes"] if value else None for value in (before, after)]
            if current not in allowed:
                raise ContentError("recovery_conflict", "A file changed after interruption; recovery preserved it.", {"path": op["path"]})
            if before:
                backup = self._history_path(before["artifact"], journal["transaction"])
                if self._current_hash(backup) != before["hashes"]:
                    raise ContentError("recovery_conflict", "The recovery backup is missing or changed.", {"path": op["path"]})
        for index, op in enumerate(journal["operations"]):
            path, before = self._path(op["path"]), op["before"]
            if self._current_hash(path) == (before["hashes"] if before else None):
                continue
            if before:
                backup = self._history_path(before["artifact"], journal["transaction"])
                temporary = self._history_path(f"history/{journal['transaction']}/restore-{index}", journal["transaction"])
                if temporary.exists():
                    # Leftover restore staging is private, but still refuse links.
                    _tree_hash(temporary)
                    if temporary.is_dir():
                        shutil.rmtree(temporary)
                    else:
                        temporary.unlink()
                self._copy_verified(backup, temporary, before["hashes"])
                path.parent.mkdir(parents=True, exist_ok=True)
                _no_links(path)
                if path.is_dir():
                    self._delete(path)
                os.replace(temporary, path)
                _sync_dir(path.parent)
            else:
                self._delete(path)
        _write_json(self.manifest_path, journal["beforeManifest"])
        self._finish_journal(journal, "rolled_back")

    def _recover(self):
        if not os.path.lexists(self.journal_path):
            return
        self._check_running()
        if not self.can_recover():
            raise ContentError('recovery_requires_private', 'Recover this interrupted transaction from its original independent installation before editing shared content.')
        try:
            journal = self._validate_journal(_bounded_json(self.journal_path))
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            raise ContentError("journal_invalid", "The interrupted transaction journal cannot be read.") from exc
        complete = self._manifest() == journal["afterManifest"] and all(self._current_hash(self._path(op["path"])) == (op["after"]["hashes"] if op["after"] else None) for op in journal["operations"])
        if complete:
            self._finish_journal(journal, "committed")
        else:
            self._rollback(journal)

    def transact(self, changes, expected_revision=None):
        try:
            owned = download_control.current()
            with (owned.noninterruptible('Applying the content transaction') if owned else download_control.bind(None)):
                return self._transact(changes, expected_revision)
        except OSError as exc:
            raise ContentError("io_error", "Content could not be staged; existing files were preserved.", {"cause": str(exc)}) from exc

    def _transact(self, changes, expected_revision):
        with self._lock:
            self._root_guard()
            self._recover()
            baseline = self._scan()
            if expected_revision is not None and expected_revision != baseline["revision"]:
                raise ContentError("stale_revision", "Installation content changed; refresh the inventory before applying.", {"revision": baseline["revision"]})
            self._check_running()
            for problem in baseline["problems"]:
                if problem["code"] in ("unsafe_path", "path_alias", "unreadable_content", "file_limit"):
                    raise ContentError("unsafe_path", "Existing content has unsafe or ambiguous paths.", problem)
            before_manifest = self._manifest()
            proposed, sources, after_manifest = self._proposal(changes, baseline, before_manifest)
            before_paths = {item["path"]: item for item in baseline["items"]}
            after_paths = {item["path"]: item for item in proposed}
            affected = [path for path in sorted(set(before_paths) | set(after_paths)) if before_paths.get(path, {}).get("hashes") != after_paths.get(path, {}).get("hashes")]
            if not affected and before_manifest == after_manifest:
                return baseline
            transaction = uuid.uuid4().hex
            after_manifest = {**after_manifest, "transaction": transaction}
            history = _no_links(self.private_dir / "history" / transaction)
            history.mkdir(parents=True)
            journal = {"schema": 1, "root": str(self.root), "transaction": transaction,
                "beforeManifest": before_manifest, "afterManifest": after_manifest, "operations": []}
            # Preparation changes private staging/history only, never live files.
            for index, relative in enumerate(affected):
                operation = {"path": relative, "before": None, "after": None}
                before, after = before_paths.get(relative), after_paths.get(relative)
                if before:
                    self._editable(before)
                    artifact = f"history/{transaction}/before-{index}"
                    self._copy_verified(self._path(relative), self._history_path(artifact, transaction), before["hashes"])
                    operation["before"] = {"artifact": artifact, "hashes": before["hashes"]}
                if after:
                    artifact = f"history/{transaction}/after-{index}"
                    source = sources.get(after["id"])
                    if source is None:
                        raise ContentError("invalid_change", "The proposed file has no verified staging source.")
                    self._copy_verified(source, self._history_path(artifact, transaction), after["hashes"])
                    operation["after"] = {"artifact": artifact, "hashes": after["hashes"]}
                journal["operations"].append(operation)
            if self._scan()["revision"] != baseline["revision"]:
                raise ContentError("stale_revision", "Installation content changed while the transaction was staged.")
            self._check_running()
            self._validate_journal(journal)
            _write_json(history / "transaction.json", journal)
            _write_json(self.journal_path, journal)
            try:
                for operation in journal["operations"]:
                    self._root_guard()
                    self._check_running()
                    path = self._path(operation["path"])
                    before, after = operation["before"], operation["after"]
                    if self._current_hash(path) != (before["hashes"] if before else None):
                        raise ContentError("local_changed", "A live file changed before its commit.", {"path": operation["path"]})
                    if after:
                        stage = self._history_path(after["artifact"], transaction)
                        if self._current_hash(stage) != after["hashes"]:
                            raise ContentError("hash_mismatch", "The staged file changed before its commit.")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        _no_links(path)
                        if path.is_dir():
                            self._delete(path)
                        os.replace(stage, path)
                        _sync_dir(path.parent)
                    else:
                        self._delete(path)
                self._check_running()
                if self._manifest() != before_manifest:
                    raise ContentError("local_changed", "Saved metadata changed during the commit.")
                _write_json(self.manifest_path, after_manifest)
                if any(self._current_hash(self._path(op["path"])) != (op["after"]["hashes"] if op["after"] else None) for op in journal["operations"]):
                    raise ContentError("local_changed", "Live content did not match the committed manifest.")
                self._finish_journal(journal, "committed")
            except Exception as exc:
                try:
                    self._rollback(journal)
                except Exception as recovery:
                    raise ContentError("recovery_conflict", "The transaction failed; backups and journal were retained for recovery.", {"cause": str(exc), "recovery": str(recovery)}) from exc
                if isinstance(exc, ContentError):
                    raise
                raise ContentError("commit_failed", "Content commit failed and the previous state was restored.", {"cause": str(exc)}) from exc
            return self._scan()
