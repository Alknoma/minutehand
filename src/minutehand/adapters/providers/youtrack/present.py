"""The stored instance as YouTrack answers it: every entity as its `*Out` model, before `fields=` narrows it."""

from __future__ import annotations

from minutehand.adapters.providers.youtrack import fields, wire
from minutehand.adapters.providers.youtrack.state import YouTrackWorld


class Presenter:
    """One request's view of the instance: what it reads of the instance-wide register is read once."""

    def __init__(self, world: YouTrackWorld) -> None:
        self._world = world
        self._definitions: list[wire.StoredFieldDefinition] | None = None
        self._projects: list[wire.StoredProject] | None = None
        self._users: dict[str, wire.StoredUser] = {}

    def _all_definitions(self) -> list[wire.StoredFieldDefinition]:
        if self._definitions is None:
            self._definitions = self._world.definitions()
        return self._definitions

    def _all_projects(self) -> list[wire.StoredProject]:
        if self._projects is None:
            self._projects = self._world.projects()
        return self._projects

    # ------------------------------------------------------------------ users

    def stored_user(self, user: str) -> wire.StoredUser:
        if user not in self._users:
            found = self._world.user(user)
            if found is None:
                raise LookupError(f"user {user} is named by an entity and does not exist")
            self._users[user] = found
        return self._users[user]

    def user(self, user: wire.StoredUser) -> wire.UserOut:
        return wire.UserOut(
            id=user.id,
            login=user.login,
            fullName=user.fullName,
            name=user.fullName,
            email=user.email,
            ringId=user.ringId,
            avatarUrl=f"/hub/api/rest/avatar/{user.ringId}?s=48",
            banned=user.banned,
        )

    def me(self, user: wire.StoredUser) -> wire.MeOut:
        out = self.user(user)
        return wire.MeOut.model_validate(out.model_dump(exclude={"type_"}))

    def user_by_id(self, user: str) -> wire.UserOut:
        return self.user(self.stored_user(user))

    # ------------------------------------------------------------------ fields

    def definition(self, definition: wire.StoredFieldDefinition) -> wire.CustomFieldOut:
        instances = [
            wire.ProjectFieldRefOut(
                type_=wire.PROJECT_FIELD_TYPES[definition.fieldType], id=field.id, project=self.project_ref(project)
            )
            for project in self._all_projects()
            for field in project.fields
            if field.field == definition.id
        ]
        return wire.CustomFieldOut(
            id=definition.id,
            name=definition.name,
            fieldType=wire.FieldTypeOut(id=definition.fieldType.value, presentation=definition.fieldType.value),
            ordinal=next((n for n, d in enumerate(self._all_definitions()) if d.id == definition.id), 0),
            instances=instances,
        )

    def _definition_of(self, field: wire.StoredProjectField) -> wire.StoredFieldDefinition:
        definition = next((d for d in self._all_definitions() if d.id == field.field), None)
        if definition is None:
            raise LookupError(f"project field {field.id} names field {field.field}, which the instance has not got")
        return definition

    def bundle_value(self, kind: wire.FieldType, value: wire.StoredBundleValue) -> wire.BundleValueOut:
        if kind is wire.FieldType.STATE:
            return wire.StateValueOut(id=value.id, name=value.name, isResolved=value.isResolved, ordinal=value.ordinal)
        if kind is wire.FieldType.VERSION:
            return wire.VersionValueOut(id=value.id, name=value.name, ordinal=value.ordinal)
        return wire.EnumValueOut(id=value.id, name=value.name, ordinal=value.ordinal)

    def bundle(self, project: wire.StoredProject, field: wire.StoredProjectField) -> wire.BundleOut | None:
        definition = self._definition_of(field)
        if field.bundle is None:
            return None
        name = f"{project.name}: {definition.name}"
        if definition.fieldType is wire.FieldType.USER:
            return wire.UserBundleOut(
                id=field.bundle,
                name=name,
                aggregatedUsers=self.team(project),
                groups=[wire.UserGroupRefOut(id=project.teamGroup, name=f"{project.name} Team")],
            )
        values = [self.bundle_value(definition.fieldType, v) for v in field.values]
        if definition.fieldType is wire.FieldType.STATE:
            return wire.StateBundleOut(
                id=field.bundle, name=name, values=[v for v in values if isinstance(v, wire.StateValueOut)]
            )
        if definition.fieldType is wire.FieldType.VERSION:
            return wire.VersionBundleOut(
                id=field.bundle, name=name, values=[v for v in values if isinstance(v, wire.VersionValueOut)]
            )
        return wire.EnumBundleOut(
            id=field.bundle, name=name, values=[v for v in values if isinstance(v, wire.EnumValueOut)]
        )

    def project_field(self, project: wire.StoredProject, field: wire.StoredProjectField) -> wire.ProjectFieldOut:
        definition = self._definition_of(field)
        defaults = [self.bundle_value(definition.fieldType, v) for v in field.values if v.id == field.defaultValue]
        return wire.ProjectFieldOut(
            type_=wire.PROJECT_FIELD_TYPES[definition.fieldType],
            id=field.id,
            field=self.definition(definition),
            project=self.project_ref(project),
            bundle=self.bundle(project, field),
            canBeEmpty=field.canBeEmpty,
            emptyFieldText=field.emptyFieldText,
            ordinal=project.fields.index(field),
            defaultValues=defaults,
        )

    def issue_value(
        self, project: wire.StoredProject, field: wire.StoredProjectField, value: wire.FieldValue | None
    ) -> wire.IssueFieldValueOut | None:
        if value is None:
            return None
        kind = self._definition_of(field).fieldType
        if kind in wire.BUNDLED:
            found = next((v for v in field.values if v.id == value), None)
            return None if found is None else self.bundle_value(kind, found)
        if kind is wire.FieldType.USER:
            return self.user_by_id(str(value))
        if kind is wire.FieldType.PERIOD:
            minutes = int(value)
            return wire.PeriodOut(id=str(minutes), minutes=minutes, presentation=fields.period_presentation(minutes))
        return value

    def issue_fields(self, project: wire.StoredProject, issue: wire.StoredIssue) -> list[wire.IssueFieldOut]:
        return [self.issue_field(project, issue, field) for field in project.fields]

    def issue_field(
        self, project: wire.StoredProject, issue: wire.StoredIssue, field: wire.StoredProjectField
    ) -> wire.IssueFieldOut:
        definition = self._definition_of(field)
        return wire.IssueFieldOut(
            type_=wire.ISSUE_FIELD_TYPES[definition.fieldType],
            id=field.id,
            name=definition.name,
            value=self.issue_value(project, field, issue.values.get(field.id)),
            projectCustomField=self.project_field(project, field),
        )

    # ------------------------------------------------------------------ projects

    def project_ref(self, project: wire.StoredProject) -> wire.ProjectRefOut:
        return wire.ProjectRefOut(id=project.id, name=project.name, shortName=project.shortName)

    def team(self, project: wire.StoredProject) -> list[wire.UserOut]:
        return [self.user_by_id(u) for u in project.team]

    def team_group(self, project: wire.StoredProject) -> wire.UserGroupOut:
        team = self.team(project)
        return wire.UserGroupOut(
            type_="ProjectTeam",
            id=project.teamGroup,
            name=f"{project.name} Team",
            ringId=None,
            usersCount=len(team),
            users=team,
        )

    def project(self, project: wire.StoredProject) -> wire.ProjectOut:
        return wire.ProjectOut(
            id=project.id,
            name=project.name,
            shortName=project.shortName,
            description=project.description,
            leader=self.user_by_id(project.leader),
            createdBy=self.user_by_id(project.createdBy),
            team=self.team_group(project),
            customFields=[self.project_field(project, f) for f in project.fields],
        )

    def home(self, issue: wire.StoredIssue) -> wire.StoredProject:
        project = next((p for p in self._all_projects() if p.id == issue.project), None)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        return project

    # ------------------------------------------------------------------ issues

    def issue_ref(self, issue: wire.StoredIssue) -> wire.LinkedIssueOut:
        return wire.LinkedIssueOut(
            id=issue.id,
            idReadable=issue.idReadable,
            numberInProject=issue.numberInProject,
            summary=issue.summary,
            description=issue.description,
            created=issue.created,
            updated=issue.updated,
            resolved=issue.resolved,
            project=self.project_ref(self.home(issue)),
        )

    def comment(self, comment: wire.StoredComment, issue: wire.StoredIssue) -> wire.CommentOut:
        return wire.CommentOut(
            id=comment.id,
            text=comment.text,
            textPreview=comment.text,
            author=self.user_by_id(comment.author),
            created=comment.created,
            updated=comment.updated,
            issue=self.issue_ref(issue),
        )

    def tag(self, tag: wire.StoredTag) -> wire.TagOut:
        return wire.TagOut(id=tag.id, name=tag.name, owner=self.user_by_id(tag.owner))

    def link_type(self, link_type: wire.StoredLinkType) -> wire.LinkTypeOut:
        return wire.LinkTypeOut(
            id=link_type.id,
            name=link_type.name,
            sourceToTarget=link_type.sourceToTarget,
            targetToSource=link_type.targetToSource,
            directed=link_type.directed,
            aggregation=link_type.aggregation,
        )

    def links(self, issue: wire.StoredIssue) -> list[wire.LinkOut]:
        """Every link slot of the issue, empty ones too, as YouTrack lists them: a directed type has an outward
        (`<type>s`) and an inward (`<type>t`) slot, an undirected one a single slot named by the type's own id."""
        edges = self._world.links()
        slots: list[wire.LinkOut] = []
        for link_type in self._world.link_types():
            mine = [e for e in edges if e.linkType == link_type.id]
            outward = [e.target for e in mine if e.source == issue.id]
            inward = [e.source for e in mine if e.target == issue.id]
            shaped = (
                [(link_type.id, wire.LinkDirection.BOTH, outward + inward)]
                if not link_type.directed
                else [
                    (f"{link_type.id}s", wire.LinkDirection.OUTWARD, outward),
                    (f"{link_type.id}t", wire.LinkDirection.INWARD, inward),
                ]
            )
            for slot, direction, ends in shaped:
                issues = [self.issue_ref(i) for i in (self._world.issue(e) for e in ends) if i is not None]
                slots.append(
                    wire.LinkOut(
                        id=slot,
                        direction=direction,
                        linkType=self.link_type(link_type),
                        issues=issues,
                        trimmedIssues=issues,
                    )
                )
        return slots

    def issue(self, issue: wire.StoredIssue) -> wire.IssueOut:
        project = self.home(issue)
        comments = [self.comment(c, issue) for c in self._world.comments(issue.id)]
        custom = self.issue_fields(project, issue)
        tags = [self.tag(t) for t in (self._world.tag(i) for i in issue.tags) if t is not None]
        return wire.IssueOut(
            id=issue.id,
            idReadable=issue.idReadable,
            numberInProject=issue.numberInProject,
            summary=issue.summary,
            description=issue.description,
            wikifiedDescription=issue.description or "",
            project=self.project(project),
            reporter=self.user_by_id(issue.reporter),
            updater=self.user_by_id(issue.updater),
            created=issue.created,
            updated=issue.updated,
            resolved=issue.resolved,
            customFields=custom,
            fields=custom,
            comments=comments,
            commentsCount=len(comments),
            tags=tags,
            links=self.links(issue),
        )
