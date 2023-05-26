# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

import yaml

from pants.backend.nfpm.field_sets import NfpmPackageFieldSet
from pants.backend.nfpm.fields.contents import (
    NfpmContentDirDstField,
    NfpmContentDstField,
    NfpmContentFileGroupField,
    NfpmContentFileModeField,
    NfpmContentFileMtimeField,
    NfpmContentFileOwnerField,
    NfpmContentSrcField,
    NfpmContentSymlinkDstField,
    NfpmContentSymlinkSrcField,
    NfpmContentTypeField,
)
from pants.core.goals.package import TraverseIfNotPackageTarget
from pants.engine.fs import CreateDigest, FileContent
from pants.engine.internals.native_engine import Digest
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.target import Target, TransitiveTargets, TransitiveTargetsRequest
from pants.engine.unions import UnionMembership
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class RequestNfpmPackageConfig:
    field_set: NfpmPackageFieldSet


@dataclass(frozen=True)
class NfpmPackageConfig:
    digest: Digest  # digest contains nfpm.yaml


class OctalInt(int):
    # noinspection PyUnusedLocal
    @staticmethod
    def represent_octal(dumper: yaml.representer.BaseRepresenter, data: int) -> yaml.Node:
        # YAML 1.2 octal: 0o7777 (py: f"0o{data:o}" or f"{data:#o}" or oct(data))
        # YAML 1.1 octal: 07777 (py: f"0{data:o}")
        # Both octal reprs are supported by `gopkg.in/yaml.v3` which parses YAML in nFPM.
        # See: https://github.com/go-yaml/yaml/tree/v3.0.1#compatibility
        # PyYAML only supports reading YAML 1.1, so we use that.
        return yaml.ScalarNode("tag:yaml.org,2002:int", f"0{data:o}")


# This is an unfortunate import-time side effect: PyYAML does registration globally.
yaml.add_representer(OctalInt, OctalInt.represent_octal)


class NfpmFileInfo(TypedDict, total=False):
    # nFPM allows these to be None or missing.
    # Each of the fields have a default, so in practice, these won't be None.
    owner: str | None
    group: str | None
    mode: OctalInt | None
    mtime: str | None


def file_info(target: Target) -> NfpmFileInfo:
    mode = target[NfpmContentFileModeField].value
    return NfpmFileInfo(
        owner=target[NfpmContentFileOwnerField].value,
        group=target[NfpmContentFileGroupField].value,
        mode=OctalInt(mode) if mode is not None else mode,
        mtime=target[NfpmContentFileMtimeField].value,
    )


class NfpmContent(TypedDict, total=False):
    src: str
    dst: str
    type: str
    packager: str
    file_info: NfpmFileInfo


@rule(level=LogLevel.DEBUG)
async def generate_nfpm_yaml(
    request: RequestNfpmPackageConfig, union_membership: UnionMembership
) -> NfpmPackageConfig:
    transitive_targets = await Get(
        TransitiveTargets,
        TransitiveTargetsRequest(
            [request.field_set.address],
            should_traverse_deps_predicate=TraverseIfNotPackageTarget(
                roots=[request.field_set.address],
                union_membership=union_membership,
            ),
        ),
    )

    # Fist get the config that can be constructed from the target itself.
    nfpm_package_target = transitive_targets.roots[0]
    config = request.field_set.nfpm_config(nfpm_package_target)

    # Second, gather package contents from hydrated deps.
    contents: list[NfpmContent] = config["contents"]

    # assumption: TransitiveTargets is AFTER target generation (so there are no target generators)
    for tgt in transitive_targets.dependencies:
        if tgt.has_field(NfpmContentDirDstField):  # an NfpmContentDir
            dst = tgt[NfpmContentDirDstField].value
            if dst is None:
                continue
            contents.append(
                NfpmContent(
                    type="dir",
                    dst=dst,
                    file_info=file_info(tgt),
                )
            )
        elif tgt.has_field(NfpmContentSymlinkDstField):  # an NfpmContentSymlink
            src = tgt[NfpmContentSymlinkSrcField].value
            dst = tgt[NfpmContentSymlinkDstField].value
            if src is None or dst is None:
                continue
            contents.append(
                NfpmContent(
                    type="symlink",
                    src=src,
                    dst=dst,
                    file_info=file_info(tgt),
                )
            )
        elif tgt.has_field(NfpmContentDstField):  # an NfpmContentFile
            src = tgt[NfpmContentSrcField].value
            dst = tgt[NfpmContentDstField].value
            # TODO: handle the 'source' field that can implicitly provide 'src'
            if src is None or dst is None:
                continue
            contents.append(
                NfpmContent(
                    type=tgt[NfpmContentTypeField].value or NfpmContentTypeField.default,
                    src=src,
                    dst=dst,
                    file_info=file_info(tgt),
                )
            )

    contents.sort(key=lambda d: d["dst"])

    nfpm_yaml = "# Generated by Pantsbuild\n"
    nfpm_yaml += yaml.safe_dump(config)
    nfpm_yaml_content = FileContent("nfpm.yaml", nfpm_yaml.encode())

    digest = await Get(Digest, CreateDigest([nfpm_yaml_content]))
    return NfpmPackageConfig(digest)


def rules():
    return [*collect_rules()]
