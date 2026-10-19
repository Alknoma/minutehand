# Firebase Cloud Messaging: where each behaviour comes from

**Documented** means Google's public reference says so (linked). **Observed** means it rests on Google's
machine-readable surface or its own client library as installed (`firebase-admin` 7.7, and the FCM v1 discovery
document `google-api-python-client` builds its client from, committed as
`tests/data/firebase_cloud_messaging/fcm_v1_discovery.json`), or on a recorded answer of the real service, read or
exercised in the tests. Nothing here is a choice the reference leaves open: where the reference is silent, the call
is refused 501 (UNIMPLEMENTED), naming what is not served.

Tests are in `tests/providers/firebase_cloud_messaging/`: `test_fcm.py` (M) and `test_fcm_coverage.py` (C); the
sign-in routing is `tests/serve/test_google_user_credentials.py` (S).

**Authentication is out of scope (`docs/design.md`, "Authentication is out of scope").** No access token is read: any
`Authorization`, or none, is answered (C `test_any_credential_or_none_is_answered`). A server's access token is minted
by Google's token endpoint, which the Google Workspace provider answers; the world that minted it is the world its
sends reach (S).

## The surface

| Claim | Class | Test | Source |
|---|---|---|---|
| REST: the 6 methods of the v1 discovery document (revision 20260911); `projects.messages.send` served and the 5 `registrations.topicSubscriptions` methods refused 501 naming the method id and why | observed | C `test_every_rest_method_is_served_or_refused_by_name_on_its_own_route`, `test_each_refused_rest_method_called_through_the_proxy_answers_501_naming_it` | `tests/data/firebase_cloud_messaging/fcm_v1_discovery.json` |
| A route that is no method of the document (the batch endpoint `/batch`, the legacy `/fcm/send`) answers 501 naming the route | observed | C `test_a_route_that_is_no_method_of_the_v1_api_answers_501_naming_it` | `tests/data/firebase_cloud_messaging/fcm_v1_discovery.json` |
| Every field of `Message` and `SendMessageRequest` in the document is read | observed | C `test_every_message_field_of_the_document_is_read` | `tests/data/firebase_cloud_messaging/fcm_v1_discovery.json` |
| A field is read by its lowerCamelCase name or its proto name: `firebase-admin` sends `validate_only` and `fcm_options`, a discovery client `validateOnly` | documented | M `test_a_dry_run_validates_as_a_send_does_and_delivers_nothing` | https://protobuf.dev/programming-guides/json/ |
| `send_each_for_multicast` sends one `messages:send` per token, never the batch endpoint, each answered on its own | observed | M `test_a_multicast_is_one_send_per_token_and_each_outcome_is_firebase_admins_own` | `firebase_admin/messaging.py` (`_MessagingService.send_each`), https://firebase.google.com/docs/cloud-messaging/send/admin-sdk |

## Sending

| Claim | Class | Test | Source |
|---|---|---|---|
| A delivered message answers `{"name": "projects/{project}/messages/{message_id}"}`; the id has the form of the reference's example (`0:1500415314455276%31bd1c9631bd1c96`): the moment in microseconds and 16 hex digits drawn from the project and the log's position; nothing more is documented and a client must not parse it | documented | M `test_a_multicast_is_one_send_per_token_and_each_outcome_is_firebase_admins_own` | https://firebase.google.com/docs/cloud-messaging/send-message |
| `validate_only` validates as a send does ("testing the request without actually delivering the message"): an unregistered token is still refused, and nothing is delivered | documented | M `test_a_dry_run_validates_as_a_send_does_and_delivers_nothing` | https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages/send |
| A message sets exactly one of `token`, `fid`, `topic`, `condition` ("mutually exclusive"; "Required"); none or two is 400 INVALID_ARGUMENT | documented | M `test_a_message_with_no_target_or_two_is_refused_invalid_argument` | https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages |
| A message to a topic, a condition or a Firebase Installation ID answers 501 naming the target: a world's devices are its seeded registration tokens | documented | M `test_a_message_to_a_topic_or_a_condition_is_refused_by_name` | https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages |
| A `data` value that is not a string is 400 INVALID_ARGUMENT, "Invalid value at 'message.data[N].value' (TYPE_STRING), V", with a `google.rpc.BadRequest` naming the field | documented | M `test_a_data_value_that_is_not_a_string_is_refused_as_fcm_words_it` | https://firebase.google.com/docs/cloud-messaging/error-codes |
| `data`'s keys and values together are at most 4096 bytes; more is 400 INVALID_ARGUMENT | documented | M `test_a_payload_over_4096_bytes_or_a_reserved_key_is_refused_and_4096_is_sent` | https://firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode |
| A reserved `data` key (`from`, `message_type`, `google.*`, `gcm.notification.*`) is 400 INVALID_ARGUMENT | documented | M the same | https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages |
| A token FCM never issued is 400 INVALID_ARGUMENT, "The registration token is not a valid FCM registration token", with an `FcmError` of `errorCode` INVALID_ARGUMENT; `firebase-admin` raises `InvalidArgumentError` | documented | M `test_a_multicast_is_one_send_per_token_and_each_outcome_is_firebase_admins_own` | https://firebase.google.com/docs/cloud-messaging/error-codes |
| An unregistered token is 404 NOT_FOUND with an `FcmError` of `errorCode` UNREGISTERED; `firebase-admin` raises `UnregisteredError` | documented | M the same | https://firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode |
| That 404's message is "Requested entity was not found." | observed | M the same | https://community.privacyidea.org/t/firebase-access-suddenly-broken-partially/2018 |
| A message's `token` is the device's address, not a credential: the record keeps it as sent | documented | M `test_a_multicast_is_one_send_per_token_and_each_outcome_is_firebase_admins_own` | https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages |

## What it does not do

- **Topics, conditions and Firebase Installation IDs**, and topic subscriptions (`registrations.topicSubscriptions`,
  and the Instance ID API at `iid.googleapis.com`), answer 501 by name or are not claimed.
- **Platform overrides** (`android`, `apns`, `webpush.data`, `webpush.notification`) are kept verbatim with the
  message and not validated; the 4096-byte limit is counted over `data` alone, as the error page words it.
- **`SENDER_ID_MISMATCH`, `QUOTA_EXCEEDED`, `THIRD_PARTY_AUTH_ERROR`** are never answered: no seed holds a sender,
  a quota or a platform credential.
