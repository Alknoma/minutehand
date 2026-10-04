"""The Slack Web API and Events API, over the run's store and clock.

`manifest.MANIFEST` is the only import the proxy makes before Slack's first call;
`provider.build()` is the one it makes on that call.
"""
