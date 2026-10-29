"""Encode inline icons for the firmware observed by existing health probes.

AWTRIX NG 1.2.2 rejects bare base64 GIF/JPEG icons. Keep renderers and their
byte budgets unchanged, and adapt a copy at the HTTP boundary. Unknown firmware
keeps the legacy format until a normal health probe identifies the device.
This pure encoding module makes no panel requests. A transport must observe
health before its first bare inline-image push, rather than guessing the format.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import re
import threading

_versions = {}
_lock = threading.Lock()


def observe_device(base_url, body):
    """Remember valid firmware versions, including a downgrade after rollback."""
    if not isinstance(body, Mapping):
        return
    version = body.get("version")
    if not isinstance(version, str):
        return
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return
    with _lock:
        _versions[str(base_url).rstrip("/")] = tuple(map(int, match.groups()))


def inline_image_mime(icon):
    """Identify a bare inline image; names, invalid data and existing URLs are not one."""
    if not isinstance(icon, str) or icon.startswith('data:'):
        return None
    try:
        raw = base64.b64decode(icon, validate=True)
    except (ValueError, binascii.Error):
        return None
    if raw.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if raw.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    return None


def prepare_payload(base_url, payload):
    """Return an independent wire payload; filenames and data URLs pass through."""
    body = dict(payload)
    with _lock:
        version = _versions.get(str(base_url).rstrip("/"), (0, 0, 0))
    icon = body.get("icon")
    if version < (1, 2, 2):
        return body
    mime = inline_image_mime(icon)
    if mime is not None:
        body['icon'] = f'data:image/{mime};base64,' + icon
    return body
