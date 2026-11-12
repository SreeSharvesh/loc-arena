"""A machine that re-signs outbound HTTPS: its bundle goes to every image build and to the gateway."""

from __future__ import annotations

from pathlib import Path

import certifi
import pytest
from loc_arena.config import load_run_config
from loc_arena.episode_stack import ComposeDocument, StackError, render_compose

CONFIG = load_run_config("configs/aurora-efficiency.yaml")
ROOTS_VARIABLE = "SSL_CERT_FILE"  # OpenSSL's name for a machine's own bundle


def render() -> ComposeDocument:
    return render_compose(CONFIG, Path("/repository"), "a-run", ["--mode", "honest"])


def list_build_secrets(compose: ComposeDocument) -> list[list[str] | None]:
    """The secrets of each image build in ``compose``; ``None`` for a build that is given none."""
    return [service["build"].get("secrets") for service in compose["services"].values() if "build" in service]


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine whose ``SSL_CERT_FILE`` names its bundle."""
    bundle = tmp_path / "machine-roots.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv(ROOTS_VARIABLE, str(bundle))
    return bundle


@pytest.fixture
def no_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine that names no bundle of its own."""
    monkeypatch.delenv(ROOTS_VARIABLE, raising=False)


def test_every_image_build_is_given_the_machines_bundle_as_its_one_secret(bundle: Path) -> None:
    compose = render()

    builds = list_build_secrets(compose)
    assert builds
    assert all(secrets == ["ca_bundle"] for secrets in builds)
    assert compose["secrets"]["ca_bundle"] == {"file": str(bundle)}


def test_the_gateway_verifies_the_provider_with_the_machines_bundle(bundle: Path) -> None:
    compose = render()

    assert compose["secrets"]["provider_roots"] == {"file": str(bundle)}


@pytest.mark.usefixtures("no_bundle")
def test_on_a_machine_with_no_bundle_no_image_build_is_given_a_secret() -> None:
    compose = render()

    assert set(list_build_secrets(compose)) == {None}
    assert "ca_bundle" not in compose["secrets"]


@pytest.mark.usefixtures("no_bundle")
def test_on_a_machine_with_no_bundle_the_gateway_verifies_the_provider_with_certifis_roots() -> None:
    compose = render()

    assert compose["secrets"]["provider_roots"] == {"file": certifi.where()}


def test_a_bundle_that_is_no_file_is_refused_naming_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.crt"
    monkeypatch.setenv(ROOTS_VARIABLE, str(missing))

    with pytest.raises(StackError, match=f"SSL_CERT_FILE names {missing}, which is no file"):
        render()
