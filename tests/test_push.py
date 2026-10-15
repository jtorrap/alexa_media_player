"""Tests for push normalization, notification toasts, and safe diagnostics."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
from unittest.mock import MagicMock

import pytest

from custom_components.alexa_media.push import (
    NotificationToastReceiver,
    RenderingUpdate,
    describe_push,
    iter_rendering_updates,
    log_push_diagnostics,
)

LOGGER_NAME = "custom_components.alexa_media.push"
TOAST_RESOURCE = "PUSH_NOTIFICATION_TOAST"


def _message(*updates):
    return {
        "directive": {
            "header": {"namespace": "Alexa.Mobile.Push", "name": "RenderUpdate"},
            "payload": {"renderingUpdates": list(updates)},
        }
    }


def _toast(notification_id="synthetic-toast"):
    return RenderingUpdate(
        resource_id=TOAST_RESOURCE,
        metadata={"payload": {"notificationId": notification_id}},
        payload={"notificationId": notification_id},
        command=None,
    )


@pytest.mark.parametrize("metadata_string", [False, True])
@pytest.mark.parametrize("payload_string", [False, True])
def test_normalizes_web_payload_formats(metadata_string, payload_string):
    """A toast does not require a command or a JSON-string payload."""
    payload = {"notificationId": "synthetic-toast"}
    metadata = {"payload": json.dumps(payload) if payload_string else payload}
    item = {
        "resourceId": TOAST_RESOURCE,
        "resourceMetadata": json.dumps(metadata) if metadata_string else metadata,
    }

    updates = iter_rendering_updates(_message(item))

    assert len(updates) == 1
    assert updates[0].resource_id == TOAST_RESOURCE
    assert updates[0].payload == payload
    assert updates[0].command is None


def test_legacy_media_command_and_payload_are_preserved_without_mutation():
    """Normalization preserves the existing media dispatch inputs."""
    payload = {
        "dopplerId": {"deviceSerialNumber": "synthetic-device", "deviceType": "test"},
        "audioPlayerState": "PLAYING",
    }
    metadata = {
        "command": "PUSH_AUDIO_PLAYER_STATE",
        "timeStamp": 123456789,
        "payload": json.dumps(payload),
    }
    message = _message(
        {
            "resourceId": "PUSH_AUDIO_PLAYER_STATE",
            "resourceMetadata": json.dumps(metadata),
        }
    )
    original = deepcopy(message)

    updates = iter_rendering_updates(message)

    assert len(updates) == 1
    assert updates[0].command == "PUSH_AUDIO_PLAYER_STATE"
    assert updates[0].payload == payload
    assert updates[0].metadata == metadata
    assert message == original


@pytest.mark.parametrize("command", [None, 7, False, [], {}])
def test_resource_id_never_becomes_a_command(command):
    metadata = {"payload": {"notificationId": "synthetic-toast"}}
    if command is not None:
        metadata["command"] = command

    update = iter_rendering_updates(
        _message({"resourceId": TOAST_RESOURCE, "resourceMetadata": metadata})
    )[0]

    assert update.command is None


@pytest.mark.parametrize(
    "message",
    [
        None,
        [],
        "not a message",
        {},
        {"directive": None},
        {"directive": []},
        {"directive": {"payload": None}},
        {"directive": {"payload": {"renderingUpdates": None}}},
        {"directive": {"payload": {"renderingUpdates": {}}}},
    ],
)
def test_invalid_outer_structure_is_ignored(message):
    assert iter_rendering_updates(message) == []


def test_malformed_items_do_not_hide_a_later_valid_update():
    invalid = [
        None,
        [],
        "not an item",
        {},
        {"resourceMetadata": "{broken"},
        {"resourceMetadata": '{"payload":' + "9" * 5000 + "}"},
        {"resourceMetadata": "null"},
        {"resourceMetadata": []},
        {"resourceMetadata": {"payload": "{broken"}},
        {"resourceMetadata": {"payload": "[]"}},
        {"resourceMetadata": {"payload": None}},
        {"resourceMetadata": {"payload": 5}},
    ]
    valid = {
        "resourceId": TOAST_RESOURCE,
        "resourceMetadata": {"payload": {"notificationId": "after-invalid"}},
    }

    updates = iter_rendering_updates(_message(*invalid, valid))

    assert len(updates) == 1
    assert updates[0].payload == {"notificationId": "after-invalid"}


def test_toast_event_has_generic_identity_and_utc_receipt_time():
    """A notification toast is not evidence of an announcement."""
    hass = MagicMock()
    receiver = NotificationToastReceiver("config-one")
    before = datetime.now(UTC)

    assert receiver.async_receive(hass, _toast()) is True

    hass.bus.async_fire.assert_called_once()
    event_type, data = hass.bus.async_fire.call_args.args
    assert event_type == "alexa_media_notification_toast"
    assert set(data) == {
        "config_entry_id",
        "resource_id",
        "notification_id",
        "received_at",
    }
    assert data["config_entry_id"] == "config-one"
    assert data["resource_id"] == TOAST_RESOURCE
    assert data["notification_id"] == "synthetic-toast"
    received_at = datetime.fromisoformat(data["received_at"])
    assert received_at.utcoffset() == timedelta(0)
    assert before <= received_at <= datetime.now(UTC)


@pytest.mark.parametrize("resource_id", [None, "PUSH_NOTIFICATION_CHANGE", "other"])
def test_non_toast_resources_are_left_for_existing_dispatch(resource_id):
    hass = MagicMock()
    update = _toast()
    update = RenderingUpdate(
        resource_id=resource_id,
        metadata=update.metadata,
        payload=update.payload,
        command=TOAST_RESOURCE,
    )

    assert NotificationToastReceiver("config-one").async_receive(hass, update) is False
    hass.bus.async_fire.assert_not_called()


@pytest.mark.parametrize(
    "notification_id", [None, "", " \t\n", 5, True, [], {}, "x" * 1025]
)
def test_invalid_id_is_consumed_without_firing(notification_id):
    hass = MagicMock()

    assert (
        NotificationToastReceiver("config-one").async_receive(
            hass, _toast(notification_id)
        )
        is True
    )
    hass.bus.async_fire.assert_not_called()


def test_maximum_length_id_is_accepted():
    hass = MagicMock()

    assert NotificationToastReceiver("config-one").async_receive(
        hass, _toast("x" * 1024)
    )
    hass.bus.async_fire.assert_called_once()


def test_duplicate_toasts_are_consumed_and_receivers_are_account_scoped():
    hass = MagicMock()
    first = NotificationToastReceiver("config-one")
    second = NotificationToastReceiver("config-two")

    assert first.async_receive(hass, _toast()) is True
    assert first.async_receive(hass, _toast()) is True
    assert hass.bus.async_fire.call_count == 1
    assert second.async_receive(hass, _toast()) is True
    assert hass.bus.async_fire.call_count == 2
    assert [
        call.args[1]["config_entry_id"] for call in hass.bus.async_fire.call_args_list
    ] == [
        "config-one",
        "config-two",
    ]


def test_deduplication_does_not_keep_an_unbounded_history():
    hass = MagicMock()
    receiver = NotificationToastReceiver("config-one")
    for index in range(129):
        assert receiver.async_receive(hass, _toast(f"toast-{index}")) is True

    # The oldest ID has fallen outside the 128-ID window.
    assert receiver.async_receive(hass, _toast("toast-0")) is True
    assert hass.bus.async_fire.call_count == 130


def _private_message():
    canaries = [f"PRIVATE_CANARY_{index}" for index in range(12)]
    return (
        {
            "directive": {
                "header": {"namespace": canaries[0], "name": canaries[1]},
                "payload": {
                    "renderingUpdates": [
                        {
                            "resourceId": canaries[2],
                            "resourceMetadata": {
                                canaries[3]: canaries[4],
                                "command": canaries[5],
                                "payload": {
                                    "notificationId": canaries[6],
                                    "text": canaries[7],
                                    "announcementText": canaries[8],
                                    "transcript": canaries[9],
                                    canaries[10]: canaries[11],
                                },
                            },
                        }
                    ]
                },
            }
        },
        canaries,
    )


@pytest.mark.parametrize("metadata_string", [False, True])
@pytest.mark.parametrize("payload_string", [False, True])
def test_projection_never_exports_private_values_or_dynamic_key_names(
    metadata_string, payload_string
):
    message, canaries = _private_message()
    item = message["directive"]["payload"]["renderingUpdates"][0]
    metadata = item["resourceMetadata"]
    if payload_string:
        metadata["payload"] = json.dumps(metadata["payload"])
    if metadata_string:
        item["resourceMetadata"] = json.dumps(metadata)
    original = deepcopy(message)

    summary = describe_push(message)

    assert isinstance(summary, dict)
    assert all(canary not in str(summary) for canary in canaries)
    assert message == original

    # Repeat with recognized routing: privacy must not depend on rejecting the envelope.
    message["directive"]["header"] = {
        "namespace": "Alexa.Mobile.Push",
        "name": "RenderUpdate",
    }
    message["directive"]["payload"]["renderingUpdates"][0][
        "resourceId"
    ] = TOAST_RESOURCE
    assert all(canary not in str(describe_push(message)) for canary in canaries)


def test_invalid_json_diagnostic_does_not_export_source_or_exception_text(caplog):
    canary = "PRIVATE_INVALID_JSON_CANARY"
    message = _message({"resourceMetadata": '{"payload": "' + canary})
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        log_push_diagnostics(message)

    assert canary not in str(describe_push(message))
    assert canary not in caplog.text


@pytest.mark.parametrize("in_directive", [False, True])
def test_announcement_envelope_presence_is_visible_without_rendering_updates(
    in_directive,
):
    """A new envelope must remain observable before the normal parser drops it."""
    canary = "PRIVATE_ANNOUNCEMENT_BODY"
    envelope = {"target": "ANNOUNCEMENT", "amznMessage": canary, canary: canary}
    message = {"directive": {"payload": envelope}} if in_directive else envelope

    summary = describe_push(message)

    assert summary["announcement_target"] is True
    assert summary["updates"] == []
    assert canary not in str(summary)


def test_diagnostic_logging_is_debug_only_and_private(caplog):
    message, canaries = _private_message()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        log_push_diagnostics(message)
    assert not [record for record in caplog.records if record.name == LOGGER_NAME]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        log_push_diagnostics(message)

    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert records
    assert all(record.levelno == logging.DEBUG for record in records)
    assert all(canary not in caplog.text for canary in canaries)
