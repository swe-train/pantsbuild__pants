# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from dataclasses import dataclass

import yaml

from pants.backend.nfpm.field_sets import NfpmPackageFieldSet
from pants.engine.addresses import Addresses
from pants.engine.fs import CreateDigest, FileContent
from pants.engine.internals.native_engine import Digest
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.target import Targets
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class RequestNfpmPackageConfig:
    field_set: NfpmPackageFieldSet


@dataclass(frozen=True)
class NfpmPackageConfig:
    digest: Digest  # digest contains nfpm.yaml


@rule(level=LogLevel.DEBUG)
async def generate_nfpm_yaml(request: RequestNfpmPackageConfig) -> NfpmPackageConfig:
    nfpm_targets = await Get(Targets, Addresses((request.field_set.address,)))
    tgt = nfpm_targets.expect_single()

    config = request.field_set.nfpm_config(tgt)

    nfpm_yaml = "# Generated by Pantsbuild\n"
    nfpm_yaml += yaml.safe_dump(config)
    nfpm_yaml_content = FileContent("nfpm.yaml", nfpm_yaml.encode())

    digest = await Get(Digest, CreateDigest([nfpm_yaml_content]))
    return NfpmPackageConfig(digest)


def rules():
    return [*collect_rules()]