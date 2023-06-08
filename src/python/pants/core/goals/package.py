# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import os
from abc import ABCMeta
from dataclasses import dataclass
from typing import Iterable

from pants.core.util_rules import distdir
from pants.core.util_rules.distdir import DistDir
from pants.core.util_rules.environments import EnvironmentNameRequest
from pants.engine.addresses import Address
from pants.engine.environment import EnvironmentName
from pants.engine.fs import Digest, MergeDigests, Workspace
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.rules import Get, MultiGet, collect_rules, goal_rule, rule
from pants.engine.target import (
    AllTargets,
    AsyncFieldMixin,
    Dependencies,
    FieldSet,
    FieldSetsPerTarget,
    FieldSetsPerTargetRequest,
    NoApplicableTargetsBehavior,
    ShouldTraverseDepsPredicate,
    SpecialCasedDependencies,
    StringField,
    Target,
    TargetRootsToFieldSets,
    TargetRootsToFieldSetsRequest,
    Targets,
)
from pants.engine.unions import UnionMembership, union
from pants.util.docutil import bin_name
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet
from pants.util.strutil import help_text

logger = logging.getLogger(__name__)


@union(in_scope_types=[EnvironmentName])
class PackageFieldSet(FieldSet, metaclass=ABCMeta):
    """The fields necessary to build an asset from a target."""


@dataclass(frozen=True)
class BuiltPackageArtifact:
    """Information about artifacts in a built package.

    Used for logging information about the artifacts that are dumped to the distdir.
    """

    relpath: str | None
    extra_log_lines: tuple[str, ...] = tuple()


@dataclass(frozen=True)
class BuiltPackage:
    digest: Digest
    artifacts: tuple[BuiltPackageArtifact, ...]


class OutputPathField(StringField, AsyncFieldMixin):
    alias = "output_path"
    help = help_text(
        f"""
        Where the built asset should be located.

        If undefined, this will use the path to the BUILD file, followed by the target name.
        For example, `src/python/project:app` would be `src.python.project/app.ext`.

        When running `{bin_name()} package`, this path will be prefixed by `--distdir` (e.g. `dist/`).

        Warning: setting this value risks naming collisions with other package targets you may have.
        """
    )

    def value_or_default(self, *, file_ending: str | None) -> str:
        if self.value:
            return self.value
        target_name_part = (
            self.address.generated_name.replace(".", "_")
            if self.address.generated_name
            else self.address.target_name
        )
        params_sanitized = self.address.parameters_repr.replace(".", "_")
        file_prefix = f"{target_name_part}{params_sanitized}"

        if file_ending is None:
            file_name = file_prefix
        else:
            assert not file_ending.startswith("."), "`file_ending` should not start with `.`"
            file_name = f"{file_prefix}.{file_ending}"
        return os.path.join(self.address.spec_path.replace(os.sep, "."), file_name)


@dataclass(frozen=True)
class EnvironmentAwarePackageRequest:
    """Request class to request a `BuiltPackage` in an environment-aware fashion."""

    field_set: PackageFieldSet


@rule
async def environment_aware_package(request: EnvironmentAwarePackageRequest) -> BuiltPackage:
    environment_name = await Get(
        EnvironmentName,
        EnvironmentNameRequest,
        EnvironmentNameRequest.from_field_set(request.field_set),
    )
    package = await Get(
        BuiltPackage, {request.field_set: PackageFieldSet, environment_name: EnvironmentName}
    )
    return package


class PackageSubsystem(GoalSubsystem):
    name = "package"
    help = "Create a distributable package."

    @classmethod
    def activated(cls, union_membership: UnionMembership) -> bool:
        return PackageFieldSet in union_membership


class Package(Goal):
    subsystem_cls = PackageSubsystem
    environment_behavior = Goal.EnvironmentBehavior.USES_ENVIRONMENTS


class AllPackageableTargets(Targets):
    pass


@rule(desc="Find all packageable targets in project", level=LogLevel.DEBUG)
async def find_all_packageable_targets(all_targets: AllTargets) -> AllPackageableTargets:
    fs_per_target = await Get(
        FieldSetsPerTarget, FieldSetsPerTargetRequest(PackageFieldSet, all_targets)
    )
    return AllPackageableTargets(
        target
        for target, field_sets in zip(all_targets, fs_per_target.collection)
        if len(field_sets) > 0
    )


@goal_rule
async def package_asset(workspace: Workspace, dist_dir: DistDir) -> Package:
    target_roots_to_field_sets = await Get(
        TargetRootsToFieldSets,
        TargetRootsToFieldSetsRequest(
            PackageFieldSet,
            goal_description="the `package` goal",
            no_applicable_targets_behavior=NoApplicableTargetsBehavior.warn,
        ),
    )
    if not target_roots_to_field_sets.field_sets:
        return Package(exit_code=0)

    packages = await MultiGet(
        Get(BuiltPackage, EnvironmentAwarePackageRequest(field_set))
        for field_set in target_roots_to_field_sets.field_sets
    )

    merged_digest = await Get(Digest, MergeDigests(pkg.digest for pkg in packages))
    all_relpaths = [
        artifact.relpath for pkg in packages for artifact in pkg.artifacts if artifact.relpath
    ]

    workspace.write_digest(
        merged_digest, path_prefix=str(dist_dir.relpath), clear_paths=all_relpaths
    )
    for pkg in packages:
        for artifact in pkg.artifacts:
            msg = []
            if artifact.relpath:
                msg.append(f"Wrote {dist_dir.relpath / artifact.relpath}")
            msg.extend(str(line) for line in artifact.extra_log_lines)
            if msg:
                logger.info("\n".join(msg))
    return Package(exit_code=0)


@dataclass(frozen=True)
class TraverseIfNotPackageTarget(ShouldTraverseDepsPredicate):
    package_field_set_types: FrozenOrderedSet[PackageFieldSet]
    roots: FrozenOrderedSet[Address]
    always_traverse_roots: bool = True  # traverse roots even if they are package targets

    def __init__(
        self,
        *,
        union_membership: UnionMembership,
        roots: Iterable[Address],
        always_traverse_roots: bool = True,
    ) -> None:
        object.__setattr__(self, "package_field_set_types", union_membership.get(PackageFieldSet))
        object.__setattr__(self, "roots", FrozenOrderedSet(roots))
        object.__setattr__(self, "always_traverse_roots", always_traverse_roots)
        self.__post_init__()

    def __call__(self, target: Target, field: Dependencies | SpecialCasedDependencies) -> bool:
        if isinstance(field, SpecialCasedDependencies):
            return False
        if self.always_traverse_roots and target.address in self.roots:
            return True
        for field_set_type in self.package_field_set_types:
            if field_set_type.is_applicable(target):
                # False means do not traverse dependencies of this target
                return False
        return True



def rules():
    return (*collect_rules(), *distdir.rules())
