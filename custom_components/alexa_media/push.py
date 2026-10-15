"""Normalize incoming push resources and inspect their structure safely.

Notification toasts are generic Amazon notifications, not proof of an Alexa
announcement or of an Echo starting to speak. No inbox request is made here.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from json import loads
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

NOTIFICATION_TOAST_RESOURCE = "PUSH_NOTIFICATION_TOAST"
NOTIFICATION_TOAST_EVENT = "alexa_media_notification_toast"
_MAX_SEEN_TOASTS = 128
_MAX_DIAGNOSTIC_UPDATES = 32
_SAFE_NAMESPACES = frozenset(
    {"Alexa.Mobile.Push", "Alexa.Comms.MessagingController", "Alexa.Notifications"}
)
_SAFE_NAMES = frozenset({"RenderUpdate", "SetIndicator", "ClearIndicator"})
_SAFE_RESOURCES = frozenset({NOTIFICATION_TOAST_RESOURCE})
_SAFE_COMMANDS = frozenset(
    {
        "PUSH_AUDIO_PLAYER_STATE",
        "PUSH_MEDIA_CHANGE",
        "PUSH_MEDIA_PROGRESS_CHANGE",
        "NotifyMediaSessionsUpdated",
        "NotifyNowPlayingUpdated",
        "PUSH_VOLUME_CHANGE",
        "PUSH_DOPPLER_CONNECTION_CHANGE",
        "PUSH_EQUALIZER_STATE_CHANGE",
        "PUSH_BLUETOOTH_STATE_CHANGE",
        "PUSH_MEDIA_QUEUE_CHANGE",
        "PUSH_NOTIFICATION_CHANGE",
        "PUSH_CONTENT_FOCUS_CHANGE",
    }
)
_SAFE_METADATA_KEYS = frozenset({"command", "payload", "timeStamp"})
_SAFE_MESSAGE_KEYS = frozenset({"directive", "target", "amznMessage"})
_SAFE_PAYLOAD_KEYS = frozenset(
    {
        "notificationId",
        "target",
        "amznMessage",
        "announcementId",
        "announcementText",
        "transcript",
        "payload",
        "type",
    }
)


@dataclass(frozen=True)
class RenderingUpdate:
    """A decoded resource, retaining the original command semantics."""

    resource_id: str | None
    metadata: dict[str, Any]
    payload: dict[str, Any]
    command: str | None


def _as_object(value: object) -> dict | None:
    """Accept objects and JSON-encoded objects without logging invalid input."""
    if isinstance(value, str):
        try:
            value = loads(value)
        except (ValueError, RecursionError):
            return None
    return value if isinstance(value, dict) else None


def _raw_updates(message_obj: object) -> list:
    """Return resources only when the directive has the expected containers."""
    if not isinstance(message_obj, dict):
        return []
    directive = message_obj.get("directive")
    if not isinstance(directive, dict):
        return []
    payload = directive.get("payload")
    if not isinstance(payload, dict):
        return []
    updates = payload.get("renderingUpdates")
    return updates if isinstance(updates, list) else []


def iter_rendering_updates(message_obj: object) -> list[RenderingUpdate]:
    """Decode resources independently so a malformed item cannot block others."""
    result = []
    for item in _raw_updates(message_obj):
        if not isinstance(item, dict):
            continue
        metadata = _as_object(item.get("resourceMetadata"))
        if metadata is None:
            continue
        payload = _as_object(metadata.get("payload"))
        if payload is None:
            continue
        resource_id = item.get("resourceId")
        command = metadata.get("command")
        result.append(
            RenderingUpdate(
                resource_id if isinstance(resource_id, str) else None,
                metadata,
                payload,
                command if isinstance(command, str) else None,
            )
        )
    return result


def _safe_enum(value: object, allowed: frozenset[str]) -> str:
    """Never return unrecognized values or their representations."""
    if value is None:
        return "missing"
    return value if isinstance(value, str) and value in allowed else "other"


def _container_type(value: object) -> str:
    """Use fixed type labels instead of arbitrary input or class names."""
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "other"


def describe_push(message_obj: object) -> dict:
    """Project allowlisted structure only, including resources AMP may ignore.

    This intentionally omits all IDs, text, arbitrary key names, account data,
    and exception messages. Unknown enum values become fixed labels.
    """
    directive = message_obj.get("directive") if isinstance(message_obj, dict) else None
    directive = directive if isinstance(directive, dict) else {}
    message = message_obj if isinstance(message_obj, dict) else {}
    header = directive.get("header")
    header = header if isinstance(header, dict) else {}
    raw_directive_payload = directive.get("payload")
    directive_payload = (
        raw_directive_payload if isinstance(raw_directive_payload, dict) else {}
    )
    raw_updates = _raw_updates(message_obj)
    summaries = []
    for item in raw_updates[:_MAX_DIAGNOSTIC_UPDATES]:
        item = item if isinstance(item, dict) else {}
        raw_metadata = item.get("resourceMetadata")
        metadata = _as_object(raw_metadata)
        raw_payload = metadata.get("payload") if metadata is not None else None
        payload = _as_object(raw_payload)
        summaries.append(
            {
                "resource_id": _safe_enum(item.get("resourceId"), _SAFE_RESOURCES),
                "metadata_type": _container_type(raw_metadata),
                "metadata_valid": metadata is not None,
                "metadata_fields": sorted(
                    _SAFE_METADATA_KEYS.intersection(metadata or {})
                ),
                "command": _safe_enum((metadata or {}).get("command"), _SAFE_COMMANDS),
                "payload_type": _container_type(raw_payload),
                "payload_valid": payload is not None,
                "payload_fields": sorted(
                    _SAFE_PAYLOAD_KEYS.intersection(payload or {})
                ),
                "notification_id_present": "notificationId" in (payload or {}),
                "announcement_target": (payload or {}).get("target") == "ANNOUNCEMENT",
            }
        )
    return {
        "message_fields": sorted(_SAFE_MESSAGE_KEYS.intersection(message)),
        "namespace": _safe_enum(header.get("namespace"), _SAFE_NAMESPACES),
        "name": _safe_enum(header.get("name"), _SAFE_NAMES),
        "directive_payload_type": _container_type(raw_directive_payload),
        "directive_payload_fields": sorted(
            _SAFE_PAYLOAD_KEYS.intersection(directive_payload)
        ),
        "announcement_target": message.get("target") == "ANNOUNCEMENT"
        or directive_payload.get("target") == "ANNOUNCEMENT",
        "updates": summaries,
        "updates_truncated": len(raw_updates) > _MAX_DIAGNOSTIC_UPDATES,
    }


def log_push_diagnostics(message_obj: object) -> None:
    """Use the dedicated push logger without enabling raw integration logs."""
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "Incoming push structure at %s: %s",
            datetime.now(UTC).isoformat(),
            describe_push(message_obj),
        )


class NotificationToastReceiver:
    """Expose generic toast IDs once per bounded, account-scoped cache.

    Reconnects on this account retain the cache; unloading the account removes
    it. This is not persistent exactly-once delivery or an announcement detector.
    """

    def __init__(self, config_entry_id: str) -> None:
        """Keep correlation independent of account emails and device serials."""
        self._config_entry_id = config_entry_id
        self._seen: OrderedDict[str, None] = OrderedDict()

    def async_receive(self, hass: HomeAssistant, update: RenderingUpdate) -> bool:
        """Consume exact toast resources without entering raw command logging."""
        if update.resource_id != NOTIFICATION_TOAST_RESOURCE:
            return False
        notification_id = update.payload.get("notificationId")
        if (
            not isinstance(notification_id, str)
            or not notification_id.strip()
            or len(notification_id) > 1024
            or notification_id in self._seen
        ):
            return True
        self._seen[notification_id] = None
        if len(self._seen) > _MAX_SEEN_TOASTS:
            self._seen.popitem(last=False)
        hass.bus.async_fire(
            NOTIFICATION_TOAST_EVENT,
            {
                "config_entry_id": self._config_entry_id,
                "resource_id": NOTIFICATION_TOAST_RESOURCE,
                "notification_id": notification_id,
                "received_at": datetime.now(UTC).isoformat(),
            },
        )
        return True
