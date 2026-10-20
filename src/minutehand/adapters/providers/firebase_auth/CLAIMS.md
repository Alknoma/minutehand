# Firebase Authentication: where each behaviour comes from

**Documented** means Google's public reference says so (linked; `REST` abbreviates
https://firebase.google.com/docs/reference/rest/auth, `admin` https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts,
`verify` https://firebase.google.com/docs/auth/admin/verify-id-tokens). **Observed** means it rests on Google's
machine-readable surface (Identity Toolkit v1's discovery document, revision 20260311, committed as
`tests/data/firebase_auth/identitytoolkit_v1_discovery.json`) or on Google's own client library as installed
(`firebase-admin` 7.7). Where the reference is silent, the call is refused 501, naming what is not served.

Tests are in `tests/providers/firebase_auth/test_firebase_auth.py`, through stock `firebase_admin` on Application
Default Credentials of type `authorized_user` and plain REST calls, each world one project of a `minutehand serve`.

**Authentication is out of scope (`docs/design.md`, "Authentication is out of scope").** The Admin SDK's bearer
token and the web API key are never checked: any, or none, is answered. A custom token's signature is not checked
either: Firebase refuses one not signed by a service account of the project with `INVALID_CUSTOM_TOKEN` or
`CREDENTIAL_MISMATCH` (REST, "Exchange custom token for an ID and refresh token"), and here it is read by its claims.
What an account holds is the service's data, not a credential check, and stays: a password signs in only its
account, a disabled account signs in nowhere, a revoked session refreshes no more.

## The surface

| Claim | Class | Test | Source |
|---|---|---|---|
| The 41 methods of Identity Toolkit v1; 16 served (`accounts.signUp`, `signInWithPassword`, `signInWithCustomToken`, `signInWithIdp`, `signInWithEmailLink`, `lookup`, `update`, `delete`, `sendOobCode`; `projects.accounts`, `.lookup`, `.update`, `.delete`, `.batchDelete`, `.batchGet`, `.sendOobCode`) and 25 refused 501 naming the method (tenants by name) | observed | `test_every_method_of_the_discovery_document_is_served_or_refused_by_name` | `tests/data/firebase_auth/identitytoolkit_v1_discovery.json` |
| A request field the method has and this fake does not serve is refused 501 naming it | observed | `test_a_field_the_method_has_and_the_fake_does_not_serve_is_refused_by_name` | `tests/data/firebase_auth/identitytoolkit_v1_discovery.json` |
| `clientType`, `recaptchaVersion` and `captchaResponse` matter only when reCAPTCHA Enterprise is enabled, and `returnSecureToken` "should always be true" | observed | `test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies` | `tests/data/firebase_auth/identitytoolkit_v1_discovery.json` |
| The Admin SDK calls `/v1/projects/{projectId}/accounts`, `accounts:lookup` (by `localId`, `email`, `federatedUserId`), `accounts:update`, `accounts:delete`, `accounts:batchDelete`, `accounts:batchGet` and `accounts:sendOobCode` | observed | `test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors` | https://github.com/firebase/firebase-admin-python/blob/master/firebase_admin/_user_mgt.py (7.7, as installed) |
| An error is `{"error": {"code", "message", "errors": [{"message", "domain": "global", "reason": "invalid"}]}}`, the message an error code | documented | `test_sign_up_and_sign_in_refusals_are_firebases_codes` | https://firebase.google.com/docs/reference/rest/auth#section-error-format |
| A code may be followed by ` : ` and a sentence, which the Admin SDK reads apart; it raises `UserNotFoundError` for `USER_NOT_FOUND`, `EmailAlreadyExistsError` for `EMAIL_EXISTS`, `UidAlreadyExistsError` for `DUPLICATE_LOCAL_ID`, `EmailNotFoundError` for `EMAIL_NOT_FOUND` | observed | `test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors` | https://github.com/firebase/firebase-admin-python/blob/master/firebase_admin/_auth_utils.py (`_CODE_TO_EXC_TYPE`) |

## Accounts

| Claim | Class | Test | Source |
|---|---|---|---|
| An account created with a chosen `localId` keeps it; one taken is `DUPLICATE_LOCAL_ID`; an address taken is `EMAIL_EXISTS` | documented | `test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors` | https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts/create |
| An account created without a `localId` is given one at random | documented | `test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies` | https://firebase.google.com/docs/auth/admin/manage-users#create_a_user ("If not provided, a random uid will be automatically generated") |
| A password is at least 6 characters: `WEAK_PASSWORD` | documented | `test_sign_up_and_sign_in_refusals_are_firebases_codes` | https://cloud.google.com/identity-platform/docs/password-policy |
| `lookup` answers each account it holds of those named, and no `users` when it holds none | documented | `test_admin_calls_with_no_credential_are_routed_by_the_project_in_their_path` | https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts/lookup |
| An account answers `localId`, `email`, `emailVerified`, `displayName`, `photoUrl`, `passwordUpdatedAt`, `providerUserInfo`, `validSince` (seconds), `disabled`, `lastLoginAt` and `createdAt` (milliseconds, as strings), `customAttributes` and `lastRefreshAt` (RFC 3339) | observed | `test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies` | `tests/data/firebase_auth/identitytoolkit_v1_discovery.json` (`GoogleCloudIdentitytoolkitV1UserInfo`) |
| `update` sets `emailVerified`, `displayName`, `photoUrl`, `disableUser`, a password, `validSince` and `customAttributes`, and `deleteAttribute` clears `DISPLAY_NAME` and `PHOTO_URL`; an account it does not hold is `USER_NOT_FOUND` | documented | `test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors` | https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts/update |
| `delete` of an account it does not hold is `USER_NOT_FOUND` | documented | the same | https://firebase.google.com/docs/reference/rest/auth#section-delete-account |
| `batchDelete` with `force` deletes every account named; without it, only disabled ones, the rest reported in `errors` by index and id; an id it does not hold is no error | documented | `test_a_batch_delete_without_force_leaves_accounts_that_are_not_disabled_and_reports_them` | https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts/batchDelete; https://firebase.google.com/docs/auth/admin/manage-users#bulk_delete_users |
| `batchGet` pages by `maxResults` (1 to 1000, 20 by default) and `nextPageToken` | documented | `test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors` | https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts/batchGet |
| Custom claims are kept as `customAttributes`, a JSON object, and appear at the top level of every ID token issued afterwards | documented | `test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors` | https://firebase.google.com/docs/auth/admin/custom-claims |

## Signing up and signing in

| Claim | Class | Test | Source |
|---|---|---|---|
| `signUp` with an address and a password makes a password account and answers `idToken`, `email`, `refreshToken`, `expiresIn` "3600" and `localId` | documented | `test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies` | https://firebase.google.com/docs/reference/rest/auth#section-create-email-password |
| `signUp` with neither makes an anonymous account | documented | `test_an_anonymous_sign_up_signs_in_with_no_address` | https://firebase.google.com/docs/reference/rest/auth#section-sign-in-anonymously |
| `signUp` refusals: `EMAIL_EXISTS`, `INVALID_EMAIL`, `MISSING_PASSWORD` | documented | `test_sign_up_and_sign_in_refusals_are_firebases_codes` | https://firebase.google.com/docs/reference/rest/auth#section-create-email-password |
| `signInWithPassword` answers `localId`, `email`, `displayName`, `idToken`, `registered`, `refreshToken`, `expiresIn` | documented | `test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies` | https://firebase.google.com/docs/reference/rest/auth#section-sign-in-email-password |
| A wrong password is `INVALID_PASSWORD`, an unknown address `EMAIL_NOT_FOUND`, a disabled account `USER_DISABLED` | documented | `test_without_email_enumeration_protection_sign_in_says_which_part_was_wrong_and_is_refused` | https://firebase.google.com/docs/reference/rest/auth#section-sign-in-email-password |
| With email enumeration protection, both are `INVALID_LOGIN_CREDENTIALS`; it is on by default for projects made since 15 September 2023 (`FirebaseAuthSeed.email_enumeration_protection`, default true) | documented | `test_sign_up_and_sign_in_refusals_are_firebases_codes` | https://cloud.google.com/identity-platform/docs/admin/email-enumeration-protection |
| `signInWithCustomToken` reads `uid` and `claims`; its `aud` is Identity Toolkit's; a token that is no JWT, of another audience, expired or with no `uid` is `INVALID_CUSTOM_TOKEN` | documented | `test_a_custom_token_for_another_audience_or_expired_is_refused` | https://firebase.google.com/docs/auth/admin/create-custom-tokens#create_custom_tokens_using_a_third-party_jwt_library |
| A custom token's `uid` with no account makes one (`isNewUser`), and its `claims` are in the ID token | documented | `test_create_custom_token_on_user_credentials_signs_through_iam_as_the_runtime_service_account` | https://firebase.google.com/docs/reference/rest/auth#section-verify-custom-token |
| `create_custom_token` on credentials with no key signs through IAM `signBlob` as the address the metadata server names | observed | the same | https://github.com/firebase/firebase-admin-python/blob/master/firebase_admin/_token_gen.py (`_init_signing_provider`) |
| `signInWithIdp` with a Google ID token in `postBody` (`id_token=...&providerId=google.com`) signs in the account linked to its `sub`, or makes one; it answers `federatedId`, `providerId`, `localId`, `email`, `emailVerified`, `displayName`, `rawUserInfo` and the tokens | documented | `test_a_google_id_token_from_the_worlds_own_sign_in_signs_in_the_account_linked_to_that_person` | https://firebase.google.com/docs/reference/rest/auth#section-sign-in-with-oauth-credential |
| `generate_sign_in_with_email_link` and the other link generators send `sendOobCode` with `returnOobLink`; the link is the project's action handler, `https://{projectId}.firebaseapp.com/__/auth/action` with `apiKey`, `mode`, `oobCode`, `continueUrl` and `lang` | documented | `test_a_sign_in_link_the_admin_sdk_generates_signs_in_once` | https://firebase.google.com/docs/auth/custom-email-handler |
| `signInWithEmailLink` signs in once per code, verifying the address, and makes the account when there is none | documented | the same | https://cloud.google.com/identity-platform/docs/reference/rest/v1/accounts/signInWithEmailLink |
| A reset link for an address with no account is `EMAIL_NOT_FOUND` | documented | the same | https://firebase.google.com/docs/reference/rest/auth#section-send-password-reset-email |

## Tokens

| Claim | Class | Test | Source |
|---|---|---|---|
| An ID token is an RS256 JWT with a `kid`; `iss` `https://securetoken.google.com/{projectId}`, `aud` the project, `sub` the uid, `auth_time`, `iat`, `exp` an hour on | documented | `test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies` | https://firebase.google.com/docs/auth/admin/verify-id-tokens#verify_id_tokens_using_a_third-party_jwt_library |
| It carries `user_id`, `email`, `email_verified`, `name`, `picture` and `firebase` (`identities`, `sign_in_provider`) | documented | the same | https://firebase.google.com/docs/reference/admin/node/firebase-admin.auth.decodedidtoken |
| Its certificates are at `www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com`, x509 by key id, with `max-age` in `Cache-Control` | documented | `test_the_certificates_are_answered_with_no_world_and_verify_the_tokens_signature` | https://firebase.google.com/docs/auth/admin/verify-id-tokens#verify_id_tokens_using_a_third-party_jwt_library |
| `verify_id_token` refuses an expired token, another project's, and a custom token; with `check_revoked`, a token issued before `validSince` and a disabled account | observed | `test_verify_id_token_refuses_a_revoked_token_a_disabled_account_and_another_projects_token`, `test_verify_id_token_refuses_a_token_the_worlds_clock_issued_more_than_an_hour_ago` | https://github.com/firebase/firebase-admin-python/blob/master/firebase_admin/_token_gen.py, https://github.com/firebase/firebase-admin-python/blob/master/firebase_admin/_auth_client.py |
| `revoke_refresh_tokens` sets `validSince` to now, and a refresh token issued before it is `TOKEN_EXPIRED` | documented | `test_a_refresh_token_is_exchanged_for_an_id_token_until_its_account_is_revoked_or_disabled` | https://firebase.google.com/docs/auth/admin/manage-sessions#revoke_refresh_tokens |
| The refresh grant at `securetoken.googleapis.com/v1/token` answers `expires_in`, `token_type`, `refresh_token`, `id_token`, `user_id`, `project_id`; refusals `INVALID_REFRESH_TOKEN`, `INVALID_GRANT_TYPE`, `MISSING_REFRESH_TOKEN`, `USER_DISABLED`, `TOKEN_EXPIRED` | documented | the same | https://firebase.google.com/docs/reference/rest/auth#section-refresh-token |
| The answer also carries `access_token`, the same ID token, which the Firebase JS SDK reads | observed | the same | https://github.com/firebase/firebase-js-sdk/blob/main/packages/auth/src/api/authentication/token.ts |
| `lookup` with an ID token that is not one of the project's, or expired, is `INVALID_ID_TOKEN` | documented | `test_sign_up_and_sign_in_refusals_are_firebases_codes` | https://firebase.google.com/docs/reference/rest/auth#section-get-account-info |

## What it does not do

- **Its times are the world's clock.** An ID token's `iat`, `exp` and `auth_time` are the world's moment, and the
  Admin SDK checks them against its own machine's clock: a world whose clock is more than an hour from the
  machine's mints tokens `verify_id_token` refuses, as expired or "used too early". `revoke_refresh_tokens` writes
  the machine's time into `validSince`, so under a standing world, whose clock stands still, a session signed in
  after a revocation is still revoked until the world's clock passes that time.
- **One key for every world.** The ID-token key is derived from a fixed seed (`adapters/derived_key.py`), since the
  Admin SDK caches the certificates for hours across worlds; a token is told apart by its `aud` and `iss`.
- **Google sign-in by ID token only.** A Google `access_token` with no `id_token` is refused 501 by name: Firebase
  asks Google whose it is. Another identity provider, signing in with Google as an address another account already
  holds (Firebase's answer depends on the project's account-linking setting), and linking (`idToken` with
  `signInWithIdp`) are refused 501 by name.
- **No email is sent.** `sendOobCode` without `returnOobLink` is refused 501 by name; a password reset or address
  verification is not completed (`accounts.resetPassword`, `update` with `oobCode`).
- **Tenants, phone numbers, multi-factor, session cookies, SAML and OIDC providers, batch import** and every other
  method are refused 501 by name. So is a call naming a project the world is not.
- **An address is matched whatever its letter case** (`lookup` by `email`, sign-in, `EMAIL_EXISTS`); no page of
  Google's says how Firebase compares addresses. A generated uid is 28 letters and digits, the length of those
  Firebase generates; nothing about its form is claimed.
- **No faults are declared**, and no quota or `TOO_MANY_ATTEMPTS_TRY_LATER` is played.
