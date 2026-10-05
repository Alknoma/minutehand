"""Microsoft: the identity platform's sign-in, the Bot Framework connector Teams bots talk to, and Microsoft Graph
for Teams, SharePoint and OneDrive, over the run's store and clock.

One provider, because `graph.microsoft.com` serves Teams and files alike and a host is claimed by one manifest,
and because the tenant, its apps and its users are one directory every surface signs in against.
`manifest.MANIFEST` is the only import the proxy makes before the first call; `provider.build()` the one it makes
on that call.
"""
