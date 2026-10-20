# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EPSM Development contributors
# License: See LICENSE at the project root.
"""Installed distributions retain their license and one authoritative version."""

from importlib import metadata

import epsm_workflows


def test_installed_wheel_retains_mit_license_and_existing_holder():
    distribution = metadata.distribution("epsm_workflows")
    assert distribution.metadata["License-Expression"] == "MIT"
    notice = distribution.read_text("licenses/LICENSE")
    assert notice is not None
    assert "Copyright (c) 2026 EPSM Development contributors" in notice
    assert "Permission is hereby granted, free of charge" in notice


def test_public_version_and_distribution_metadata_agree():
    assert epsm_workflows.__version__ == metadata.version("epsm_workflows")
