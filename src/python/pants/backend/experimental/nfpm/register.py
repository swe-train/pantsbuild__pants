# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.nfpm import field_sets
from pants.backend.nfpm.rules import rules as nfpm_rules
from pants.backend.nfpm.target_types import (
    NfpmApkPackage,
    NfpmArchlinuxPackage,
    NfpmDebPackage,
    NfpmRpmPackage,
)
from pants.backend.nfpm.target_types_rules import rules as target_type_rules


def target_types():
    return [
        NfpmApkPackage,
        NfpmArchlinuxPackage,
        NfpmDebPackage,
        NfpmRpmPackage,
    ]


def rules():
    return [
        *target_type_rules(),
        *field_sets.rules(),
        *nfpm_rules(),
    ]