"""What a simulated person does, and where it is delivered."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from minutehand.domain.conversation import Provenance
from minutehand.domain.scenario import FormInput, Model, ProviderKey
from minutehand.domain.world import EntityRef


class Press(Model):
    """A control the person uses on the message they answer, instead of writing back, and the form they fill if
    the agent opens one for it."""

    action_id: str = Field(description="The control's own id, as the agent wrote it on the message")
    label: str = Field(description="What the control reads, as the person saw it")
    value: str | None = Field(default=None, description="The control's own value, as the agent wrote it")
    picks: str | None = Field(default=None, description="Person.key chosen, when the control picks a person")
    form: list[FormInput] = []


class PersonReply(Model):
    """One reply from one person, decided once and stored with the run: text written back, or a control used."""

    person: str = Field(description="Person.key")
    in_reply_to: EntityRef
    text: str = Field(description="What they said: their message, or for a press what the form holds, else the label")
    at: AwareDatetime = Field(description="Simulated time the reply lands")
    written_by: Provenance | None = Field(
        default=None, description="The model and prompt that wrote it; None is scripted"
    )
    press: Press | None = Field(default=None, description="Set when the reply is a control used, not a message")


class PersonMessage(Model):
    """Something a person says to the agent unprompted: the owner handing over the goal, or a direction."""

    person: str = Field(description="Person.key")
    text: str
    at: AwareDatetime = Field(description="Simulated time the message is sent")


VariableName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class GeneratedSecret(Model):
    """A fresh secret made for every run and handed to the agent's command in the variable `env`.

    Only a command Minutehand starts receives it; an agent already running has a secret of its own."""

    kind: Literal["generated"] = "generated"
    env: VariableName = Field(description="The variable the agent reads its signing secret from")


class SecretFromEnvironment(Model):
    """The agent's own secret, configured where it already runs: Minutehand reads the same value from its own
    variable `env` when the run starts, and refuses the run when it is not set."""

    kind: Literal["from_env"] = "from_env"
    env: VariableName = Field(description="The variable in Minutehand's own environment that holds the secret")


SigningSecret = Annotated[GeneratedSecret | SecretFromEnvironment, Field(discriminator="kind")]


class InboundTarget(Model):
    """Where a provider pushes events to the agent, the way the real service would."""

    provider: ProviderKey
    url: str
    interactivity_url: str | None = Field(
        default=None,
        description="Where a person's use of a control or a form is pushed (Slack's interactivity request URL); "
        "None: the same as `url`",
    )
    secret: SigningSecret | None = Field(
        default=None,
        description="Where the secret that signs pushed events comes from; None signs with one made per run "
        "and given to no one, for an agent that does not verify",
    )


class PermissionGrant(Model):
    """A named permission held or withheld for a person on one project (or across the instance), changed while the
    world is open. The permission is named as the provider names it (YouTrack's `jetbrains.youtrack.updateIssue`)."""

    person: str = Field(description="Person.key")
    permission: str = Field(min_length=1, description="The provider's own name for the permission")
    project: str | None = Field(default=None, description="The project's own key; None: across the instance")
    held: bool = Field(description="True grants it, False withholds it")


class InboundCredentialAsk(Model):
    """What a test that builds a pushed request itself needs signed or minted, the way the provider's real service
    would sign the request it pushes. Each provider reads the fields its own scheme needs and refuses a request
    missing one: Slack signs `body` at `timestamp` (seconds since the epoch, as `X-Slack-Request-Timestamp` carries
    it); the Bot Framework mints a token for `service_url` addressed to `audience` (the bot's app id)."""

    body: str = Field(default="", description="The request body exactly as it will be sent")
    timestamp: int | None = Field(default=None, ge=0, description="Seconds since the epoch, as the request states")
    service_url: str | None = Field(default=None, description="The Bot Framework `serviceUrl` the activity names")
    audience: str | None = Field(default=None, description="Who the token is for: the bot's app (client) id")


class Header(Model):
    name: str
    value: str


class InboundCredential(Model):
    """The headers that make a request the test builds look pushed by the provider's own service."""

    headers: list[Header] = Field(min_length=1)
