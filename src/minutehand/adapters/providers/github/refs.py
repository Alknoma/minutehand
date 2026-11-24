"""Branches as git references: making one (`POST /repos/{owner}/{repo}/git/refs`), and reading one back by its reference
(`GET .../git/ref/{ref}`) or as a branch (`GET .../branches/{branch}`).

A reference made here is a branch (`refs/heads/<name>`), kept where the repository keeps every branch
(`history.branched`): made at the default branch's head it follows the head, as a seeded branch does, until a commit
is made on it; made at a commit of a branch with commits of its own, or where one was made, it is a branch of its own.
Either is then a branch everywhere one is read or written: listed, read at, committed to (`branch`), a pull request's
`head` (https://docs.github.com/en/rest/git/refs#create-a-reference).

What GitHub answers a create it refuses is the real service's own, each a 422 in its own words, recorded on a scratch
repository (https://github.com/sapientpants/software-factory/pull/31); a name is a reference's name as git reads one
(https://git-scm.com/docs/git-check-ref-format).
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from starlette.requests import Request

from minutehand.adapters.providers.github import content, history, state, wire
from minutehand.adapters.providers.github.answers import Answered, Caller, as_json
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor, Operation

if TYPE_CHECKING:
    from minutehand.adapters.providers.github.app import GitHubApi

CREATE = "/git/refs#create-a-reference"
GET = "/git/refs#get-a-reference"
BRANCH = "/branches/branches#get-a-branch"
REFS = "refs/"
HEADS = "heads/"
SHA = re.compile(r"^([0-9a-f]{40}(?:[0-9a-f]{24})?)$")
"""A sha as GitHub takes one: the pattern its own refusal quotes (observed)."""
SHA_LENGTH = 40
NOT_IN_A_NAME = frozenset(" ~^:?*[\\")
"""What git takes in no reference's name, beside the control characters (`check-ref-format`, rules 4, 5 and 10)."""


def valid_name(ref: str) -> bool:
    """Whether git takes `ref` as a reference's name (https://git-scm.com/docs/git-check-ref-format): no component that
    is empty, begins with a dot or ends with `.lock`; no `..`; no control character, space, `~`, `^`, `:`, `?`, `*`,
    `[` or backslash; no dot at its end; no `@{`; and not `@` alone."""
    if ref == "@" or "@{" in ref or ".." in ref or ref.endswith("."):
        return False
    if any(ord(c) < 0o40 or ord(c) == 0o177 or c in NOT_IN_A_NAME for c in ref):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in ref.split("/"))


def refuse_sha(sha: str) -> None:
    """A sha GitHub's validation of the request refuses, in its words (observed): shorter than a sha, and not one."""
    if SHA.fullmatch(sha):
        return
    problems: list[str] = []
    if len(sha) < SHA_LENGTH:
        supplied = "was" if len(sha) == 1 else "were"
        problems.append(f"At least {SHA_LENGTH} characters are required; only {len(sha)} {supplied} supplied.")
    problems.append(f"{sha} does not match /{SHA.pattern}/.")
    raise wire.Refusal(422, "Invalid request.\n\n" + "\n".join(problems), section=CREATE)


class Refs:
    def __init__(self, api: GitHubApi) -> None:
        self._api = api

    # ------------------------------------------------------------------ presenting

    def ref_out(self, repository: wire.StoredRepository, branch: str, sha: str) -> wire.GitRefOut:
        full = repository.full_name
        ref = f"{REFS}{HEADS}{branch}"
        return wire.GitRefOut(
            ref=ref,
            node_id=wire.node_id("REF", int(hashlib.sha1(f"{full.lower()}\0{ref}".encode()).hexdigest()[:8], 16)),
            url=f"{wire.API}/repos/{full}/git/{ref}",
            object=wire.GitObjectOut(sha=sha, url=f"{wire.API}/repos/{full}/git/commits/{sha}"),
        )

    # ------------------------------------------------------------------ what a sha names

    def holds_object(self, repository: wire.StoredRepository, sha: str) -> bool:
        """Whether `sha` names a blob or a tree the repository holds: an object, though no commit."""
        world = self._api.world
        if world.blob(repository, sha) is not None:
            return True
        paths = set(history.trunk_tree(world, repository))
        for line in repository.lines:
            paths |= set(history.line_tree(line))
        directories = {""} | {"/".join(p.split("/")[:depth]) for p in paths for depth in range(1, p.count("/") + 1)}
        return any(content.tree_sha(repository, d) == sha for d in directories)

    # ------------------------------------------------------------------ the routes

    async def create_ref(self, request: Request, caller: Caller) -> Answered:
        """201 with the reference, its address in `Location` as the description declares. Refused, in this order: the
        request's shape (`ref` and `sha`, and a sha of a sha's form), the name's form, the object the sha names (seen
        to come before whether the reference exists), and the name against the branches the repository has."""
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, CREATE)
        self._api.tracker.agent(caller)
        sent = wire.Sent.read(await request.body(), section=CREATE)
        ref = sent.text("ref", required=True)
        assert ref is not None
        if not sent.present("sha"):
            raise wire.Refusal(422, 'Invalid request.\n\n"sha" wasn\'t supplied.', section=CREATE)
        sha = sent.text("sha", required=True)
        assert sha is not None
        refuse_sha(sha)
        if not ref.startswith(REFS):
            raise wire.Refusal(422, "Reference name must start with 'refs/'.", section=CREATE)
        if not valid_name(ref):
            raise wire.Refusal(422, f"{ref} is not a valid ref name.", section=CREATE)
        if not ref.startswith(REFS + HEADS):
            raise NotServed(f"the reference {ref}: the world holds branches (refs/heads/), not tags or other refs")
        if not repository.commits:
            raise NotServed("a reference in an empty repository: the reference says none is made there, not how")
        branch = ref.removeprefix(REFS + HEADS)
        commit = history.commit_of(repository, sha)
        if commit is None and not self.holds_object(repository, sha):
            raise wire.Refusal(422, "Object does not exist", section=CREATE)
        held = history.tips(repository)
        if branch in held:
            raise wire.Refusal(422, "Reference already exists", section=CREATE)
        if commit is None or any(n.startswith(branch + "/") or branch.startswith(n + "/") for n in held):
            # A tree or a blob, or a name that is a directory of another branch's or under one.
            raise wire.Refusal(422, "Reference update failed", section=CREATE)
        made = history.branched(repository, branch, sha)
        if made is None:
            raise NotServed(
                f"a branch at {sha}, a commit of the default branch's history before its head: only the head's files "
                "are kept, and the files of the branches with commits of their own (README)"
            )
        self._api.world.update_repository(made, Actor.AGENT, state.snapshot("refs", ref))
        out = self.ref_out(repository, branch, sha)
        return as_json(out, 201, {"Location": out.url})

    async def get_ref(self, request: Request, caller: Caller) -> Answered:
        """A branch by its reference, `heads/<branch>` exactly: anything else names no reference here (the world holds
        no tags), a 404, as a name that is only a prefix of one is (observed)."""
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, GET)
        if not repository.commits:
            raise wire.Refusal(409, "Git Repository is empty.", section=GET)
        wanted: str = request.path_params["ref"]
        branch = wanted.removeprefix(HEADS) if wanted.startswith(HEADS) else None
        held = history.tips(repository)
        if branch is None or branch not in held:
            raise wire.not_found(GET)
        self._api.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json(self.ref_out(repository, branch, held[branch]))

    async def get_branch(self, request: Request, caller: Caller) -> Answered:
        """One branch, its commit in full and the protection of a branch nobody protects; one that is not there is 404
        "Branch not found" (observed). What lies under a branch's address (its protection and the rest) is not
        served, and refused by name."""
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, BRANCH)
        wanted: str = request.path_params["branch"]
        held = history.tips(repository)
        if wanted not in held:
            under = next((n for n in held if wanted.startswith(n + "/")), None)
            if under is not None:
                raise NotServed(f"{wanted.removeprefix(under + '/')} of the branch {under}: only the branch is served")
            raise wire.Refusal(404, "Branch not found", section=BRANCH)
        commit = history.commit_of(repository, held[wanted])
        if commit is None:
            raise LookupError(f"{repository.full_name}'s branch {wanted} points at {held[wanted]}, which it has not")
        full = repository.full_name
        at = f"{wire.API}/repos/{full}/branches/{wanted}"
        links = wire.BranchLinksOut.model_validate({"self": at, "html": f"{wire.WEB}/{full}/tree/{wanted}"})
        self._api.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json(
            wire.BranchDetailOut.model_validate(
                {
                    "name": wanted,
                    "commit": self._api.commit_out(repository, commit),
                    "_links": links,
                    "protection_url": f"{at}/protection",
                }
            )
        )
