#!/usr/bin/env python3
"""Recover the preview site's remaining legacy media into its GitHub media tree.

This is a one-time, idempotent migration helper. It downloads public legacy media
from an explicit host allowlist, updates the preview JSON indexes, and records
verified local GitHub paths. The live site is never touched.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = os.environ.get("VHA_PREVIEW_BASE_PATH", "/landing-page-preview").rstrip("/")
MAX_BYTES = 90 * 1024 * 1024

OWNED_MEDIA_HOSTS = {
    "oeyxsqocldlovhsmzcjw.supabase.co",
    "ioguymqdeuuursizifnn.supabase.co",
    "emanuelstefancu.wordpress.com",
}
EXTERNAL_MEDIA_HOSTS = {
    "i.pinimg.com",
    "64.media.tumblr.com",
    "www.issues4life.org",
    "issues4life.org",
    "www2.hawaii.edu",
}
ALLOWED_HOSTS = OWNED_MEDIA_HOSTS | EXTERNAL_MEDIA_HOSTS

URL_RE = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)


def read_json(relative: str) -> dict:
    path = ROOT / relative
    if not path.is_file():
        raise RuntimeError(f"Required preview data file is missing: {relative}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(relative: str, value: dict) -> None:
    (ROOT / relative).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def safe_target(raw: str) -> str:
    value = str(raw or "").replace("\\", "/").lstrip("/")
    parts = PurePosixPath(value).parts
    if not parts or parts[0] != "media" or any(part in ("", ".", "..") for part in parts):
        raise RuntimeError(f"Refusing unsafe media target path: {raw}")
    return "/".join(parts)


def target_file(raw: str) -> Path:
    return ROOT / safe_target(raw)


def url_key(raw: str) -> str:
    parsed = urllib.parse.urlsplit(str(raw or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return ""
    return parsed.hostname.lower() + urllib.parse.unquote(parsed.path)


def host_allowed(host: str) -> bool:
    value = (host or "").lower().rstrip(".")
    if value in ALLOWED_HOSTS:
        return True
    return value.endswith(".wordpress.com") or value.endswith(".wp.com")


def source_allowed(raw: str) -> bool:
    parsed = urllib.parse.urlsplit(str(raw or ""))
    return parsed.scheme.lower() == "https" and host_allowed(parsed.hostname or "")


def add_target(
    source: str,
    target: str,
    *,
    fallbacks: list[str] | None = None,
    required: bool = True,
) -> None:
    target = safe_target(target)
    target_candidates.setdefault(target, [])
    target_required[target] = target_required.get(target, False) or required
    for candidate in [source, *(fallbacks or [])]:
        candidate = str(candidate or "").strip()
        key = url_key(candidate)
        if key and source_allowed(candidate):
            if candidate not in target_candidates[target]:
                target_candidates[target].append(candidate)
            source_to_target[key] = target


def sniff_ok(data: bytes, target: str) -> bool:
    if not data:
        return False
    head = data[:256].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"{\"error\"", b"{\"message\"")):
        return False
    suffix = PurePosixPath(target).suffix.lower()
    if suffix in (".jpg", ".jpeg") and data[:3] != b"\xff\xd8\xff":
        return False
    if suffix == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    if suffix == ".pdf" and not data.startswith(b"%PDF-"):
        return False
    if suffix == ".mp4" and b"ftyp" not in data[:32]:
        return False
    return True


def download_target(target: str, candidates: list[str]) -> tuple[bool, int]:
    destination = target_file(target)
    if destination.is_file() and destination.stat().st_size > 0:
        return True, destination.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error = "No approved source URL was available."
    for url in candidates:
        if not source_allowed(url):
            last_error = f"Source host is not approved: {urllib.parse.urlsplit(url).hostname}"
            continue
        for attempt in range(1, 4):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "VerseAfterHours-Media-Recovery/1.0",
                        "Accept": "*/*",
                    },
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    final_url = response.geturl()
                    final_host = urllib.parse.urlsplit(final_url).hostname or ""
                    if not host_allowed(final_host):
                        raise RuntimeError(f"Redirected to an unapproved host: {final_host}")
                    content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
                    if content_type in ("text/html", "application/json", "text/plain"):
                        raise RuntimeError(f"Source returned non-media content type: {content_type}")
                    data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise RuntimeError(f"File exceeds the {MAX_BYTES // (1024 * 1024)} MiB GitHub safety limit.")
                if not sniff_ok(data, target):
                    raise RuntimeError("Downloaded content did not match the expected media file type.")
                temporary = destination.with_name(destination.name + ".part")
                temporary.write_bytes(data)
                temporary.replace(destination)
                print(f"RECOVERED {target} ({len(data)} bytes; {content_type or 'unknown MIME'})")
                return True, len(data)
            except Exception as error:  # network and source errors are retried below
                last_error = str(error)
                if attempt < 3:
                    time.sleep(attempt * 2)
        print(f"SOURCE FAILED {url}: {last_error}")
    return False, 0


def replace_urls(value, replacements: dict[str, str]):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            # Preserve provenance. Only rendered/source fields are rewritten.
            if key in ("source_url", "aliases"):
                result[key] = child
            else:
                result[key] = replace_urls(child, replacements)
        return result
    if isinstance(value, list):
        return [replace_urls(item, replacements) for item in value]
    if isinstance(value, str):
        for source, destination in replacements.items():
            value = value.replace(source, destination)
        return value
    return value


def local_url(path: str) -> str:
    return BASE_PATH + "/" + safe_target(path)


# Load the three public content indexes used by the preview site.
archive = read_json("content/human-archive.json")
transmissions = read_json("content/transmissions.json")
signals = read_json("content/signals.json")

manifest = archive.get("media_manifest") or {}
manifest_items = manifest.get("items") or []
if not isinstance(manifest_items, list):
    raise RuntimeError("Human Archive media manifest is malformed.")

target_candidates: dict[str, list[str]] = {}
target_required: dict[str, bool] = {}
source_to_target: dict[str, str] = {}
archive_reference_ids: dict[str, set[int]] = {}

# Existing WordPress attachments and the three externally referenced files.
for item in manifest_items:
    target = item.get("local_path")
    source = item.get("source_url")
    if target and source:
        external = item.get("source_type") == "external-inline-reference"
        add_target(source, target, fallbacks=item.get("aliases") or [], required=not external)
        if item.get("wp_media_id") is not None and item.get("storage_path"):
            # Old editor structures store the former Supabase object URL, while
            # the recovered file inventory stores the equivalent WordPress path.
            legacy_alias = (
                "https://ioguymqdeuuursizifnn.supabase.co/storage/v1/object/public/"
                "human-archive-media/" + str(item["storage_path"]).lstrip("/")
            )
            add_target(legacy_alias, target, required=True)

# Newer Transmissions and community submissions carry their intended GitHub paths.
for collection in (transmissions, signals):
    for record in collection.get("records") or []:
        for media in record.get("media_items") or []:
            source = media.get("source_url")
            target = media.get("src") or media.get("url")
            if source and target and str(target).startswith("media/"):
                add_target(source, target, required=True)

# Inspect all saved Human Archive blocks/output media. Some newer archive records
# refer to files uploaded to the old public bucket but were never in the WP export.
for structure in archive.get("editor_structures") or []:
    record_id = structure.get("record_id")
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        record_id = 0
    strings = [block.get("html", "") for block in structure.get("blocks") or []]
    output_media = structure.get("output_media") or {}
    for group in output_media.values() if isinstance(output_media, dict) else []:
        if isinstance(group, list):
            strings.extend(str(item.get("url") or item.get("source_url") or "") for item in group if isinstance(item, dict))
    for value in strings:
        for url in URL_RE.findall(value or ""):
            key = url_key(url)
            if not key or key in source_to_target:
                if key:
                    archive_reference_ids.setdefault(key, set()).add(record_id)
                continue
            parsed = urllib.parse.urlsplit(url)
            if (parsed.hostname or "").lower() not in OWNED_MEDIA_HOSTS:
                continue
            filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            if not filename or filename in (".", "..") or not record_id:
                continue
            target = f"media/human-archive/HA-{record_id:04d}/{filename}"
            add_target(url, target, required=True)
            archive_reference_ids.setdefault(key, set()).add(record_id)

# Download only files which are absent; the operation is safe to rerun.
required_failures = []
optional_failures = []
downloaded = 0
for target, candidates in target_candidates.items():
    existed = target_file(target).is_file() and target_file(target).stat().st_size > 0
    ok, size = download_target(target, candidates)
    if ok and not existed:
        downloaded += 1
    elif not ok:
        if target_required.get(target, False):
            required_failures.append(target)
        else:
            optional_failures.append(target)

if required_failures:
    print(
        "MEDIA RECOVERY PARTIAL // These source files are unavailable and remain pending: "
        + ", ".join(required_failures)
    )


# Rebuild the URL -> preview-file substitutions now that all available assets exist.
replacements: dict[str, str] = {}
for target, candidates in target_candidates.items():
    if target_file(target).is_file() and target_file(target).stat().st_size > 0:
        destination = local_url(target)
        for candidate in candidates:
            replacements[candidate] = destination

# Verify/update the legacy media manifest and attach local storage metadata.
manifest_item_by_key: dict[str, dict] = {}
for item in manifest_items:
    target = item.get("local_path")
    if target and target_file(target).is_file() and target_file(target).stat().st_size > 0:
        item["storage"] = "github-public"
        item["recovery_status"] = "verified"
        aliases = item.setdefault("aliases", [])
        if item.get("wp_media_id") is not None and item.get("storage_path"):
            legacy_alias = (
                "https://ioguymqdeuuursizifnn.supabase.co/storage/v1/object/public/"
                "human-archive-media/" + str(item["storage_path"]).lstrip("/")
            )
            if legacy_alias not in aliases:
                aliases.append(legacy_alias)
        for value in [item.get("source_url"), *(aliases or [])]:
            key = url_key(value)
            if key:
                manifest_item_by_key[key] = item

# Add recovered objects referenced by newer Human Archive structures to the same
# manifest, so the visitor renderer and editor media library share one resolver.
added_manifest_items = 0
for key, record_ids in archive_reference_ids.items():
    if key in manifest_item_by_key:
        item = manifest_item_by_key[key]
        item["post_ids"] = sorted(set((item.get("post_ids") or []) + [value for value in record_ids if value]))
        continue
    target = source_to_target.get(key)
    if not target or not target_file(target).is_file():
        continue
    source_url = next((url for url in replacements if url_key(url) == key), "")
    if not source_url:
        continue
    filename = PurePosixPath(target).name
    item_id = "github-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    item = {
        "id": item_id,
        "wp_media_id": None,
        "post_ids": sorted(value for value in record_ids if value),
        "title": filename,
        "alt_text": "",
        "caption_html": "",
        "source_url": source_url,
        "aliases": [source_url],
        "storage_path": target,
        "mime_type": mime_type,
        "expected_size": target_file(target).stat().st_size,
        "source_type": "github-recovered-media",
        "local_path": target,
        "storage": "github-public",
        "recovery_status": "verified",
    }
    manifest_items.append(item)
    manifest_item_by_key[key] = item
    added_manifest_items += 1

# Bring all Human Archive record-level attachments into sync with the local files.
for record in archive.get("records") or []:
    for media in record.get("media_items") or []:
        target = media.get("src") or media.get("local_path") or media.get("path")
        if target and str(target).startswith("media/") and target_file(target).is_file():
            media["status"] = "available"
            media["url"] = local_url(target)
            media["path"] = safe_target(target)
            media["storage"] = "github-public"

# Rewrite saved inline HTML/output media to local URLs via their manifest aliases.
# Keep legacy source URLs in saved HTML so the archive renderer can resolve them
# through the media manifest aliases; only the URLs emitted to the browser are local.
for structure in archive.get("editor_structures") or []:
    output_media = structure.get("output_media")
    if isinstance(output_media, dict):
        for group in output_media.values():
            if not isinstance(group, list):
                continue
            for media in group:
                if not isinstance(media, dict):
                    continue
                key = url_key(media.get("url") or media.get("source_url") or "")
                target = source_to_target.get(key)
                if target and target_file(target).is_file():
                    # The editor should use the local preview/production path;
                    # source_url remains untouched as historical provenance.
                    media["url"] = local_url(target)
                    media["path"] = safe_target(target)
                    media["storage"] = "github-public"

# Update the Transmission/Signal indexes only after each file has been verified.
def mark_collection_media(collection: dict) -> int:
    count = 0
    for record in collection.get("records") or []:
        for media in record.get("media_items") or []:
            target = media.get("src") or media.get("url")
            if target and str(target).startswith("media/") and target_file(target).is_file():
                media["status"] = "available"
                media["url"] = safe_target(target)
                media["storage"] = "github-public"
                count += 1
    return count

transmission_media_count = mark_collection_media(transmissions)
signal_media_count = mark_collection_media(signals)

# Include externally hosted files in the verified local resolver only when the
# download succeeded. Unrecovered third-party references remain explicitly pending.
for item in manifest_items:
    if item.get("source_type") == "external-inline-reference":
        target = item.get("local_path")
        if target and target_file(target).is_file() and target_file(target).stat().st_size > 0:
            item["storage"] = "github-public"
            item["recovery_status"] = "verified"

manifest["items"] = manifest_items
manifest["item_count"] = len(manifest_items)
manifest["preview_recovery_revision"] = "github-media-v1"
manifest["preview_recovery_report"] = {
    "status": "partial" if required_failures or optional_failures else "verified",
    "missing_owned_media_paths": required_failures,
    "missing_external_media_paths": optional_failures,
    "newly_downloaded_files": downloaded,
}
manifest["verified_local_files"] = sum(
    1 for item in manifest_items
    if item.get("local_path") and target_file(item["local_path"]).is_file()
)
archive["media_manifest"] = manifest

write_json("content/human-archive.json", archive)
write_json("content/transmissions.json", transmissions)
write_json("content/signals.json", signals)

print(
    "PREVIEW MEDIA RECOVERY COMPLETE: "
    f"{downloaded} downloaded; "
    f"{manifest['verified_local_files']} Human Archive files verified; "
    f"{added_manifest_items} newer archive media references indexed; "
    f"{transmission_media_count} Transmission items available; "
    f"{signal_media_count} Signal items available."
)
if optional_failures:
    print("OPTIONAL THIRD-PARTY FILES NOT RECOVERED: " + ", ".join(optional_failures))
