"""Slack: the Web API, the Events API, interactivity, `response_url` and `url_private`, over the run's store and clock.

`manifest.MANIFEST` is the only import the proxy makes before Slack's first call; `provider.build()` is the one it
makes on that call. `app.py` answers what the agent calls; `inbound.py` pushes what people say and do;
`interactive.py` pushes what they press and submit; `seed.py` writes the workspace a scenario starts in. What is
covered and what is not, call by call against a production caller: `docs/design.md`, "Slack, against its
production caller".
"""
