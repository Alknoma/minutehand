"""A scenario from the library: a situation in the world every proactive agent meets, written once with the people
left as placeholders, and filled with one team's people when it writes the scenario out.

A library scenario is a world and nothing more: who is silent, who answers and when, who is away and who covers, who
decides in the agent's own product, what the scheduler gets wrong. The agent brings its own work, so a library
scenario hands it none: no goal, no owner, no expectation of what it achieves, and no rule about how it should work.
Every run of it is assessed against the agent's own instructions and the world it declares (`docs/assessments.md`).
What it leaves to the team is who its people are:

    {team.person_key} {team.person_name} {team.person_email}
                                                   the person the situation is about
    {team.other_key} {team.other_name} {team.other_email}
                                                   a second person: who covers, who decides, who writes in
    {team.knows}                                   a fact the person holds, which their answer carries
    {team.credential_env}                          the variable the agent reads an approver's sign-in from
    {team.provider}                                the messaging provider a person writes in on, unprompted
    {team.wakes}                                   how the agent asks for its own wakes: reported, booked or polled

How the agent reaches people (Slack, email, its own product) is the agent file's, never the scenario's, so it is not
a value here. Placeholders are filled inside strings only (`domain.templates.fill`): a fact holding YAML or braces
stays text.
"""

from __future__ import annotations

from email.utils import parseaddr
from typing import Self

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.common import VariableName
from minutehand.domain.scenario import Model, PlannedBy, ProviderKey, WrittenScenario
from minutehand.domain.templates import fill, named

NAMESPACE = "team"
SCALARS = ("knows", "credential_env", "provider", "wakes")
ROLES = ("person", "other")
PLACEHOLDERS = tuple(f"{NAMESPACE}.{s}" for s in SCALARS) + tuple(
    f"{NAMESPACE}.{role}_{part}" for role in ROLES for part in ("key", "name", "email")
)
"""Every placeholder a library scenario may name."""

DEFAULT_KNOWS = "The reference is RF-4410."


class WhoRefused(ValueError):
    """A person given as text that does not read as `Name <email>`."""


class Who(Model):
    """One of the team's people, as a library scenario names them."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="How findings name them: their first name, lower case")
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s<>]+@[^@\s<>]+$")

    @classmethod
    def written(cls, said: str) -> Who:
        """`Rosa Lind <rosa@example.com>`, as an address book writes a person; the key is the first name."""
        name, email = parseaddr(said)
        if not name or "@" not in email:
            raise WhoRefused(
                f"{said!r} is not a person: write it as 'Name <email>', e.g. 'Rosa Lind <rosa@example.com>'"
            )
        key = "".join(c for c in name.split()[0].casefold() if c.isascii() and c.isalnum())
        if not key or not key[0].isalpha():
            raise WhoRefused(f"{said!r}: the first name must start with a letter a to z, to name them in findings")
        return cls(key=key, name=name, email=email)


OTHER = Who(key="sam", name="Sam Okafor", email="sam@example.com")
"""The second person when the team names none."""


class TeamValues(Model):
    """Who one team's people are, to write a library scenario out: the person a situation is about, and a second one
    where a situation has two. The rest have values that work with any agent and are used only where a scenario names
    them."""

    person: Who
    other: Who = OTHER
    knows: str = Field(default=DEFAULT_KNOWS, min_length=1, description="A fact the person holds")
    credential_env: VariableName = "APPROVER_TOKEN"
    provider: ProviderKey = "slack"
    wakes: PlannedBy = PlannedBy.REPORTED

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.person.key == self.other.key or self.person.email == self.other.email:
            shared = (
                f"the key {self.person.key!r}"
                if self.person.key == self.other.key
                else f"the email {self.person.email!r}"
            )
            raise ValueError(f"person and other share {shared}; each must be a different person")
        return self

    def placeholders(self) -> dict[str, str]:
        """Each `team.<name>` and its value."""
        values = {"knows": self.knows, "credential_env": self.credential_env, "provider": self.provider}
        values |= {"wakes": self.wakes.value}
        for role, who in (("person", self.person), ("other", self.other)):
            values |= {f"{role}_key": who.key, f"{role}_name": who.name, f"{role}_email": who.email}
        return {f"{NAMESPACE}.{k}": v for k, v in values.items()}


class LibraryScenario(Model):
    """One library entry: the scenario, a world in the scenario file's own format with `{team.*}` placeholders in its
    text, and the situation it puts the agent in."""

    situation: str = Field(min_length=1, description="What happens in the world, in a few sentences")
    scenario: JsonValue = Field(description="The scenario file, its team's people as `{team.*}` placeholders")

    @model_validator(mode="after")
    def _placeholders_known(self) -> Self:
        if (
            not isinstance(self.scenario, dict)
            or "name" not in self.scenario
            or not isinstance(self.scenario["name"], str)
        ):
            raise ValueError("`scenario` is a scenario file's mapping, with its `name`")
        handed = sorted(k for k in ("goal", "owner", "deadline_after", "expect", "assess") if k in self.scenario)
        if handed:
            raise ValueError(
                f"a library scenario is a world: it hands the agent no work and judges it by no rule of its own, "
                f"so it has no {', '.join(handed)}"
            )
        unknown = sorted({n for n in named(self.scenario) if n.startswith(f"{NAMESPACE}.")} - set(PLACEHOLDERS))
        if unknown:
            raise ValueError(f"names {', '.join('{' + u + '}' for u in unknown)}, which no team value fills")
        return self

    @property
    def name(self) -> str:
        assert isinstance(self.scenario, dict) and isinstance(self.scenario["name"], str)
        return self.scenario["name"]

    @property
    def uses(self) -> list[str]:
        """The team's values this scenario names, each once, as `TeamValues` fields (`person`, not `person_email`)."""
        said = {n.removeprefix(f"{NAMESPACE}.") for n in named(self.scenario)}
        return [f for f in TeamValues.model_fields if f in said or any(s.startswith(f"{f}_") for s in said)]

    def filled(self, team: TeamValues) -> JsonValue:
        """The scenario file with the team's values in it, as it is written out."""
        return fill(self.scenario, team.placeholders())

    def written(self, team: TeamValues) -> WrittenScenario:
        """The filled scenario as a run reads it; refused, naming why, when the values make it invalid."""
        return WrittenScenario.model_validate(self.filled(team))
