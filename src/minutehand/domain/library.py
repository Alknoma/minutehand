"""A scenario from the library: a situation every proactive agent meets, written once with what differs from team to
team left as placeholders, and filled with one team's values when it writes the scenario out.

What a library scenario fixes is the situation: who is silent, who answers and when, who is away, who decides, what
the scheduler gets wrong, and what must be true at the end. What it leaves to the team is what only the team knows:

    {team.goal}                                    the goal handed to the agent, verbatim
    {team.owner_key} {team.owner_name} {team.owner_email}
                                                   who gave the goal, and is told the outcome
    {team.ask_key} {team.ask_name} {team.ask_email}
                                                   the person the agent must ask to reach the goal
    {team.answer} {team.tell}                      what that person answers, and a phrase only that answer holds,
                                                   which must reach the owner
    {team.other_key} {team.other_name} {team.other_email}
                                                   a second person: a delegate, an approver, a stranger who writes in
    {team.credential_env}                          the variable the agent reads an approver's sign-in from
    {team.provider}                                the messaging provider a person writes in on, unprompted
    {team.wakes}                                   how the agent asks for its own wakes: reported, booked or polled

How the agent reaches people (Slack, email, its own product) is the agent file's, never the scenario's, so it is not
a value here. Placeholders are filled inside strings only (`domain.templates.fill`): a goal holding YAML or braces
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
SCALARS = ("goal", "answer", "tell", "credential_env", "provider", "wakes")
ROLES = ("owner", "ask", "other")
PLACEHOLDERS = tuple(f"{NAMESPACE}.{s}" for s in SCALARS) + tuple(
    f"{NAMESPACE}.{role}_{part}" for role in ROLES for part in ("key", "name", "email")
)
"""Every placeholder a library scenario may name."""

DEFAULT_ANSWER = "Yes, that is confirmed. The reference is RF-4410."
DEFAULT_TELL = "RF-4410"


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
    """What one team supplies to write a library scenario out. Every library scenario uses the goal, the owner and
    the person asked; the others have values that work with any agent and are used only where a scenario names
    them."""

    goal: str = Field(min_length=1)
    owner: Who
    ask: Who
    other: Who = OTHER
    answer: str = Field(default=DEFAULT_ANSWER, min_length=1)
    tell: str = Field(default=DEFAULT_TELL, min_length=1, description="A phrase of `answer` that must reach the owner")
    credential_env: VariableName = "APPROVER_TOKEN"
    provider: ProviderKey = "slack"
    wakes: PlannedBy = PlannedBy.REPORTED

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.tell.casefold() not in self.answer.casefold():
            raise ValueError(
                f"the tell {self.tell!r} is not in the answer {self.answer!r}; give a phrase the answer holds"
            )
        people = (("owner", self.owner), ("ask", self.ask), ("other", self.other))
        for n, (role, who) in enumerate(people):
            for later, same in people[n + 1 :]:
                if who.key == same.key or who.email == same.email:
                    shared = f"the key {who.key!r}" if who.key == same.key else f"the email {who.email!r}"
                    raise ValueError(f"{role} and {later} share {shared}; each must be a different person")
        return self

    def placeholders(self) -> dict[str, str]:
        """Each `team.<name>` and its value."""
        values = {"goal": self.goal, "answer": self.answer, "tell": self.tell}
        values |= {"credential_env": self.credential_env, "provider": self.provider, "wakes": self.wakes.value}
        for role, who in (("owner", self.owner), ("ask", self.ask), ("other", self.other)):
            values |= {f"{role}_key": who.key, f"{role}_name": who.name, f"{role}_email": who.email}
        return {f"{NAMESPACE}.{k}": v for k, v in values.items()}


class LibraryScenario(Model):
    """One library entry: the scenario, in the scenario file's own format with `{team.*}` placeholders in its text,
    and what it is for. The rules that judge it are in the scenario (`assess`), the team's to edit once written."""

    situation: str = Field(min_length=1, description="What happens, in a few sentences")
    good_agent: str = Field(min_length=1, description="What a proactive agent does about it")
    patterns: list[str] = Field(min_length=1, description="The Pattern.key of each design it exercises")
    scenario: JsonValue = Field(description="The scenario file, its team's values as `{team.*}` placeholders")

    @model_validator(mode="after")
    def _placeholders_known(self) -> Self:
        if (
            not isinstance(self.scenario, dict)
            or "name" not in self.scenario
            or not isinstance(self.scenario["name"], str)
        ):
            raise ValueError("`scenario` is a scenario file's mapping, with its `name`")
        unknown = sorted({n for n in named(self.scenario) if n.startswith(f"{NAMESPACE}.")} - set(PLACEHOLDERS))
        if "assess" not in self.scenario or not self.scenario["assess"]:
            raise ValueError("a library scenario carries the rules that judge it, in its scenario's `assess`")
        if unknown:
            raise ValueError(f"names {', '.join('{' + u + '}' for u in unknown)}, which no team value fills")
        return self

    @property
    def name(self) -> str:
        assert isinstance(self.scenario, dict) and isinstance(self.scenario["name"], str)
        return self.scenario["name"]

    @property
    def rules(self) -> list[str]:
        """The ids of the rules its scenario judges by."""
        assert isinstance(self.scenario, dict) and isinstance(self.scenario["assess"], list)
        return [str(r["id"]) for r in self.scenario["assess"] if isinstance(r, dict)]

    @property
    def uses(self) -> list[str]:
        """The team's values this scenario names, each once, as `TeamValues` fields (`owner`, not `owner_email`)."""
        said = {n.removeprefix(f"{NAMESPACE}.") for n in named(self.scenario)}
        return [f for f in TeamValues.model_fields if f in said or any(s.startswith(f"{f}_") for s in said)]

    def filled(self, team: TeamValues) -> JsonValue:
        """The scenario file with the team's values in it, as it is written out."""
        return fill(self.scenario, team.placeholders())

    def written(self, team: TeamValues) -> WrittenScenario:
        """The filled scenario as a run reads it; refused, naming why, when the values make it invalid."""
        return WrittenScenario.model_validate(self.filled(team))
