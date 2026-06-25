import pytest
from resoluto_sandbox.images import build, image_tags, PROVIDERS


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)


def test_build_langchain_returns_correct_tag():
    fake = FakeRunner()
    tag = build("langchain", ver="9.9.9", runner=fake)
    assert tag == "resoluto-sandbox:9.9.9-langchain"


def test_build_records_base_then_overlay():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", runner=fake)
    assert len(fake.calls) == 2
    assert "Dockerfile.base" in fake.calls[0]
    assert "images/langchain.Dockerfile" in fake.calls[1]


def test_build_passes_base_image_arg():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", runner=fake)
    overlay_cmd = fake.calls[1]
    assert "--build-arg" in overlay_cmd
    base_arg_idx = overlay_cmd.index("--build-arg")
    assert overlay_cmd[base_arg_idx + 1].startswith("BASE_IMAGE=resoluto-sandbox-base:9.9.9")


def test_build_passes_image_version_arg():
    fake = FakeRunner()
    build("langchain", ver="9.9.9", runner=fake)
    overlay_cmd = fake.calls[1]
    assert "IMAGE_VERSION=9.9.9" in overlay_cmd


def test_build_custom_base_tag():
    fake = FakeRunner()
    build("openai", ver="1.0.0", base_tag="my-base:latest", runner=fake)
    assert "my-base:latest" in fake.calls[0]
    assert "BASE_IMAGE=my-base:latest" in fake.calls[1]


def test_build_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        build("bogus", ver="1.0.0", runner=FakeRunner())


def test_image_tags_shape():
    tags = image_tags("1.2")
    assert tags["base"] == "resoluto-sandbox-base:1.2"
    for p in PROVIDERS:
        assert tags[p] == f"resoluto-sandbox:1.2-{p}"


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

    from resoluto_sandbox.cli import main
    rc = main(["image", "build", "--provider", "langchain", "--version", "9.9.9"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "resoluto-sandbox:9.9.9-langchain" in out
    assert len(calls) == 2


def test_cli_image_build_context_flag_passed_through(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    from resoluto_sandbox.cli import main
    rc = main(["image", "build", "--provider", "claude", "--version", "1.0.0", "--context", ".."])
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][-1] == ".."
    assert calls[1][-1] == ".."
