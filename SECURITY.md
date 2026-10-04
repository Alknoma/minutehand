# Security

Minutehand intercepts an agent's outbound calls and answers them from fakes. Two properties are
security properties and a defect in either is a vulnerability:

- **A run must not reach a real service.** A call to a host no provider claims is refused, and a
  call to a model API passes through untouched unless the run asks for it to be edited.
- **Credentials are not stored.** Authorization headers never reach the store; credential-named
  values are removed from recorded query strings and bodies.

Report a vulnerability privately through GitHub's "Report a vulnerability" on this repository.
Do not open a public issue for one.
