# reCAPTCHA Enterprise: where each behaviour comes from

**Documented** means Google's public reference says so (linked). **Observed** means it rests on Google's
machine-readable surface as installed (the v1 discovery document `google-api-python-client` builds its client from,
committed as `tests/data/recaptcha_enterprise/recaptchaenterprise_v1_discovery.json`), read or exercised in the tests.
Nothing here is a choice the reference leaves open: where the reference is silent, the call is refused 501
(UNIMPLEMENTED), naming what is not served.

Tests are in `tests/providers/recaptcha_enterprise/`: `test_recaptcha.py` (R) and `test_recaptcha_coverage.py` (C);
the sign-in routing is `tests/serve/test_google_user_credentials.py` (S).

**Authentication is out of scope (`docs/design.md`, "Authentication is out of scope").** No access token or API key
is read: any `Authorization`, or none, is answered (C `test_any_credential_or_none_is_answered`). A backend's
access token is minted by Google's token endpoint, which the Google Workspace provider answers; the world that minted
it is the world its assessments reach (S).

**Refusal messages.** Google's own wording of these errors is in no reference, so each refusal's message is
google.rpc.Code's description of INVALID_ARGUMENT, "The client specified an invalid argument."
(https://github.com/googleapis/googleapis/blob/master/google/rpc/code.proto), followed by what was wrong.

## The surface

| Claim | Class | Test | Source |
|---|---|---|---|
| REST: the 24 methods of the v1 discovery document (revision 20260825); `projects.assessments.create` served and 23 refused 501 naming the method id and why | observed | C `test_every_rest_method_is_served_or_refused_by_name_on_its_own_route`, `test_each_refused_rest_method_called_through_the_proxy_answers_501_naming_it` | `tests/data/recaptcha_enterprise/recaptchaenterprise_v1_discovery.json` |
| Every field of `Event` and `Assessment` in the document is read: the event's strings are echoed, an unknown field is 400 INVALID_ARGUMENT | observed | C `test_every_event_field_of_the_document_is_read`; R `test_an_assessment_asking_for_more_than_a_tokens_verdict_is_refused_by_name` | `tests/data/recaptcha_enterprise/recaptchaenterprise_v1_discovery.json` |
| A field is read by its lowerCamelCase name or its proto name (`siteKey`, `site_key`); the answer spells it lowerCamelCase | documented | R `test_either_spelling_of_a_field_is_read_and_the_answer_is_in_lower_camel_case` | https://protobuf.dev/programming-guides/json/ |
| An event asking for more than a token's verdict (`transactionData`, `userInfo`, `fraudPrevention`, `express`, `wafTokenAssessment`, `firewallPolicyEvaluation`), and an assessment with `accountVerification` or `privatePasswordLeakVerification`, answers 501 naming the field: each asks for an assessment block the reference documents and the world has nothing to decide it from | documented | R `test_an_assessment_asking_for_more_than_a_tokens_verdict_is_refused_by_name` | https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments |

## The assessment

| Claim | Class | Test | Source |
|---|---|---|---|
| The answer is the Assessment: `name` (`projects/{project}/assessments/{assessment}`), the `event` echoed, `riskAnalysis` (`score`, `reasons`) and `tokenProperties` (`valid`, `invalidReason`, `createTime`, `hostname`, `action`) | documented | R `test_a_seeded_token_is_answered_with_its_verdict_and_the_event_echoed` | https://cloud.google.com/recaptcha/docs/interpret-assessment-website |
| A field at its default value is left out: `valid` false, an empty `hostname` or `action`, a score of 0, no reasons | documented | R `test_a_token_assessed_again_is_dupe_and_one_older_than_two_minutes_is_expired` | https://protobuf.dev/programming-guides/json/ |
| An assessment's id is in the documented name format; nothing more about it is documented, so it is drawn from the project and the log's position (unique in the run, the same on replay) and a client must not parse it | documented | R `test_a_seeded_token_is_answered_with_its_verdict_and_the_event_echoed` | https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments |
| `createTime` is when the token was minted ("the generation of the token"), in RFC 3339, Z-normalized | documented | R the same | https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments |
| A score is one of 11 levels, 0.0 to 1.0; a seeded score between levels is refused at seeding | documented | R `test_a_seed_naming_a_token_twice_or_a_score_between_levels_is_refused` | https://cloud.google.com/recaptcha/docs/interpret-assessment-website |
| `reasons` are `ClassificationReason` values not marked deprecated | documented | R `test_a_seeded_token_is_answered_with_its_verdict_and_the_event_echoed` | https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments |
| A token the seed does not name is `MALFORMED` ("The provided user verification token was malformed"): no browser minted it | documented | R `test_an_unseeded_token_is_malformed_and_a_missing_one_missing` | https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments#InvalidReason |
| An event with no token, or no event, is `MISSING` ("The user verification token was not present") | documented | R the same | https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments#InvalidReason |
| A seeded token assessed again is `DUPE` ("The user verification had already been seen"; "Each token can be used only once"), whatever its age | documented | R `test_a_token_assessed_again_is_dupe_and_one_older_than_two_minutes_is_expired` | https://cloud.google.com/recaptcha/docs/create-assessment-website |
| A seeded token more than two minutes old on the run's clock is `EXPIRED` ("it expires after two minutes") | documented | R the same, `test_a_token_expires_two_minutes_after_it_was_minted_on_the_runs_clock` | https://cloud.google.com/recaptcha/docs/create-assessment-website |
| `valid = false` carries the reason in `invalidReason`; the other token properties and the risk analysis are left out | documented | R `test_a_token_assessed_again_is_dupe_and_one_older_than_two_minutes_is_expired` | https://cloud.google.com/recaptcha/docs/interpret-assessment-website |
| The unseeded verdict stands for a token minted for each assessment ("call execute() again to generate a new token"): minted at the assessment, so never `DUPE` or `EXPIRED` | documented | R `test_the_unseeded_verdict_stands_for_a_fresh_token_each_time_and_is_never_dupe` | https://cloud.google.com/recaptcha/docs/create-assessment-website |
| An event's `token` is the response token, not a credential: the record keeps it as sent | documented | R `test_a_seeded_token_is_answered_with_its_verdict_and_the_event_echoed` | https://cloud.google.com/recaptcha/docs/create-assessment-website |

## Seed conveniences

These are Minutehand's, not Google's: they say what a seed stands for, and no answer depends on anything else.

- `RecaptchaSeed.unseeded.action` left out (`None`) stands for a browser on whichever page the assessment is for:
  its fresh token was minted for the action the event's `expectedAction` names, so the answer's
  `tokenProperties.action` is that one (R `test_an_unseeded_verdict_with_no_action_answers_the_action_each_assessment_expects`).
  A seeded token always names its own action.
- A seeded token's `minted_at` left out is the run's start.

## What it does not do

- **`UNEXPECTED_ACTION`, `KEY_MISMATCH`, `DOMAIN_MISMATCH`, `BROWSER_ERROR`.** Each depends on a site key's settings
  or the browser, which no seed holds; the agent compares `tokenProperties.action` with its `expectedAction` itself,
  as Google advises.
- **gRPC.** `google-cloud-recaptcha-enterprise` speaks gRPC only; this provider serves the REST API.
- **Keys, firewall policies, related account groups, annotations** answer 501 by name.
