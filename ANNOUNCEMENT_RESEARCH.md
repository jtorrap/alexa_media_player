# Incoming announcement investigation

This branch investigates receiving ordinary announcements initiated by an Echo
or Alexa app. Sending announcements is already supported by upstream AMP.
**This branch does not yet establish live announcement reception or speech-start
timing. It has not been deployed to a live Home Assistant instance.**

## What the source establishes

Checked on 2026-10-15 against upstream `dev` at `baa9ff8` and release `v5.16.2`.
Both have the same receive-side gap. The current Alexa+ website accepts this
resource shape:

```json
{
  "directive": {
    "header": { "namespace": "Alexa.Mobile.Push", "name": "RenderUpdate" },
    "payload": {
      "renderingUpdates": [
        {
          "resourceId": "PUSH_NOTIFICATION_TOAST",
          "resourceMetadata": "{\"payload\":{\"notificationId\":\"synthetic-example\"}}"
        }
      ]
    }
  }
}
```

The nested payload may also be a JSON string. The metadata need not contain
`command`. This example is a synthetic fixture matching the website parser;
it is not a captured announcement.

Upstream AMP requires `command` and tries to JSON-decode the payload even if
it is already an object. Consequently it can drop this resource before its
normal command log. The underlying alexapy 1.31.0 library already advertises
`Alexa.Mobile.Push` and `Alexa.Comms.MessagingController`; enabling a missing
capability is not the proposed fix.

Current web source: [toast handler](https://m.media-amazon.com/images/S/sash/rXzdwJd4iimiKKh.js),
[inbox listener](https://m.media-amazon.com/images/S/sash/DjrPLC66EWajJQS.js), and
[AVS worker](https://m.media-amazon.com/images/S/sash/vJZaxJyrJfU5D$H.js?chunk=71040).
These public assets may change. Their transport is the same AVS endpoint
family used by alexapy, but the website's managed token and client configuration
differ. Matching endpoints do not prove identical subscriptions.

## Change in this branch

- Decode metadata and payload as either JSON objects or JSON-encoded objects.
  Skip malformed resources independently so later valid updates still work.
- Preserve existing command routing. Do not infer commands from arbitrary
  resource IDs.
- Consume the exact `PUSH_NOTIFICATION_TOAST` resource before normal command
  logging and expose the opaque notification ID through a generic HA event.
- Keep a bounded cache of the last 128 IDs per account to suppress duplicates
  across reconnects. Unloading the account clears this cache; delivery is not
  persistent or guaranteed exactly once.
- Provide a separate DEBUG logger that reports allowlisted structure only,
  including unknown or malformed resources that do not reach command dispatch.

The functional event is `alexa_media_notification_toast`:

```yaml
config_entry_id: integration-entry-id
resource_id: PUSH_NOTIFICATION_TOAST
notification_id: opaque-amazon-notification-id
received_at: "2026-10-15T22:00:00+00:00"
```

This is a **generic notification-ID event**. It contains no message text,
announcement classification, source device or speech-start confirmation.
`received_at` is local receipt time in UTC. The code makes no inbox request,
sends no announcement and does not operate media or refresh voice history.
The opaque ID stays on the local HA event bus; it is excluded from the dedicated
diagnostic log.

## Safe diagnostic capture

If this branch is deliberately installed for a bounded investigation, enable
only the dedicated logger through HA's supported runtime action:

```yaml
action: logger.set_level
data:
  custom_components.alexa_media.push: debug
```

Restore its prior level afterward, normally `info`. Leave the parent
`custom_components.alexa_media` and `alexapy` loggers, and the integration's
separate account-debug option, at their normal levels. Existing upstream
command logs can include private payloads; enabling the parent logger defeats
the narrow capture's privacy boundary.

The new log contains receipt time, fixed namespace/name/resource/command enums,
container types, allowlisted field names and presence booleans. Unknown values
become `other`; unknown keys, IDs, text and exception contents are omitted.
It logs at most 32 resource summaries per directive. No raw payload capture is
required to answer whether a toast or recognized announcement field arrives.

## What still needs proof

1. Observe a genuine announcement on this existing connection, with a bounded
   pre-parser capture. Distinguish a typed app announcement, recorded app
   announcement and spoken Echo command. Sending an AMP notification is a
   control, not an independent-source test.
2. If a toast arrives, determine whether its ID resolves in the separate
   `/api/mobilepushnotifications/notifications` inbox using the authenticated
   session. Existing `AlexaAPI.get_notifications()` reads alarms/reminders and
   is a different service. No inbox reader has been added to this branch.
3. Require an explicit announcement type before exposing an announcement event.
   Generic toast, media, alarm and accessibility notifications are insufficient.
4. Measure local receipt relative to actual Echo speech. A creation timestamp
   or successful service call does not establish useful TV-pause latency.

The website's known inbox fallback polls every 30 seconds for Alexa+ task
results, filtering `AlexaCAMAgentOrchestrator`. Its household-announcement
coverage is unverified and that existing poller is too slow for this goal.

An older [decompiled Alexa Android app](https://github.com/Super-Crab/com.amazon.dee.app/blob/e46f4ca2cc5b4774a0c1ec7112f343c91c08833b/com.amazon.dee.app_0/sources/com/amazon/deecomms/notifications/service/CreateNotificationService.java#L201-L279)
has an explicit separate communications push path: `target: ANNOUNCEMENT`,
with `amznMessage` carrying typed text or a recorded-audio transcript. Its
communications/DMPS registration is not implemented here. If ordinary
announcements are absent from this connection and the web inbox, that separate
subscription would require a larger change than this parser extension.

## Validation

Tests in `tests/test_push.py` use synthetic data. They cover legacy media
normalization, website payload encodings, malformed-message recovery, generic
event shape, invalid IDs, account isolation, duplicate suppression, bounded
cache eviction and private canaries in both values and dynamic key names.

These tests establish parser/event behavior. They do not establish Amazon
delivery, acoustic timing or a working announcement-triggered TV automation.

Local validation on 2026-10-15: 255 tests passed using Python 3.13.11 and the
repository's locked Home Assistant 2025.4.4 test dependencies. All pre-commit
hooks passed, including formatting, import sorting, Bandit and spelling checks.
The installed live HA version was not changed or used for this test run.
