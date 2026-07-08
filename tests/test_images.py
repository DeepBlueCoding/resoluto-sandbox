import pytest
from resoluto.sandbox.images import (
    build, image_tags, pullable, PROVIDERS, SDK_PACKAGE, SDK_VERSION,
)


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)


def _builds(fake):
    """The `docker build` calls only (push adds `docker tag`/`docker push` calls)."""
    return [c for c in fake.calls if c[:2] == ["docker", "build"]]


def test_build_langchain_returns_correct_tag():
    fake = FakeRunner()
    tag = build("langchain", ver="9.9.9", runner=fake)
    assert tag == f"resoluto-sandbox:langchain-{SDK_VERSION['langchain']}"


def test_build_records_base_then_overlay():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", push=False, runner=fake)
    assert len(fake.calls) == 2
    assert "Dockerfile.base" in fake.calls[0]
    assert "images/langchain.Dockerfile" in fake.calls[1]


def test_build_pushes_base_and_overlay_to_registry():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", runner=fake)
    pushed = [c[2] for c in fake.calls if c[:2] == ["docker", "push"]]
    assert pushed == [
        pullable("resoluto-sandbox-base:9.9.9"),
        pullable(f"resoluto-sandbox:langchain-{SDK_VERSION['langchain']}"),
    ]


def test_build_passes_base_image_arg():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", push=False, runner=fake)
    overlay_cmd = _builds(fake)[1]
    assert "--build-arg" in overlay_cmd
    base_arg_idx = overlay_cmd.index("--build-arg")
    assert overlay_cmd[base_arg_idx + 1].startswith("BASE_IMAGE=resoluto-sandbox-base:9.9.9")


def test_build_passes_image_version_arg():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", push=False, runner=fake)
    assert "IMAGE_VERSION=9.9.9" in _builds(fake)[1]


def test_build_passes_sdk_version_arg():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", push=False, runner=fake)
    assert f"SDK_VERSION={SDK_VERSION['langchain']}" in _builds(fake)[1]


def test_build_custom_base_tag():
    fake = FakeRunner()
    build("openai", ver="1.0.0", base_tag="my-base:latest", push=False, runner=fake)
    assert len(_builds(fake)) == 1
    assert "BASE_IMAGE=my-base:latest" in _builds(fake)[0]


def test_build_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        build("bogus", ver="1.0.0", runner=FakeRunner())


def test_image_tags_shape():
    tags = image_tags("1.2")
    assert tags["base"] == "resoluto-sandbox-base:1.2"
    for p in PROVIDERS:
        assert tags[p] == f"resoluto-sandbox:{SDK_PACKAGE[p]}-{SDK_VERSION[p]}"


def test_build_all_providers():
    fake = FakeRunner()
    for p in PROVIDERS:
        build(p, ver="0.5.0", runner=fake)
    overlay_names = [tok for c in fake.calls for tok in c if tok.startswith("images/")]
    assert set(overlay_names) == {f"images/{p}.Dockerfile" for p in PROVIDERS}


def test_cli_image_build_langchain(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    from resoluto.sandbox.cli import main
    rc = main(["image", "build", "--provider", "langchain", "--version", "9.9.9"])
    assert rc == 0
    out = capsys.readouterr().out
    overlay = f"resoluto-sandbox:langchain-{SDK_VERSION['langchain']}"
    assert overlay in out
    assert f"pushed {pullable(overlay)}" in out
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert len(builds) == 2  # base + overlay (each also tagged + pushed)


def test_cli_image_build_all_builds_base_once(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    from resoluto.sandbox.cli import main
    rc = main(["image", "build", "--provider", "all", "--version", "1.0.0"])
    assert rc == 0
    base_calls = [c for c in calls if "Dockerfile.base" in c]
    overlay_calls = [c for c in calls if any(tok.startswith("images/") for tok in c)]
    assert len(base_calls) == 1
    assert len(overlay_calls) == len(PROVIDERS)


def test_cli_image_build_context_flag_passed_through(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    from resoluto.sandbox.cli import main
    rc = main(["image", "build", "--provider", "claude", "--version", "1.0.0", "--context", ".."])
    assert rc == 0
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert len(builds) == 2                    # base + overlay
    assert all(c[-1] == ".." for c in builds)  # context flag threaded to both
