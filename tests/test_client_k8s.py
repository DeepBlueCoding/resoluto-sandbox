"""Integration test: Sandbox(backend='k8s').run() round-trips through a real Kata pod."""
import pytest

from resoluto.sandbox import Sandbox


@pytest.mark.integration
@pytest.mark.skip(
    reason="The k8s facade's s3 store requires a worker-minted, per-prefix scoped write token "
    "(host AWS creds are never forwarded — by design). Sandbox(backend='k8s').run() generates its "
    "prefix internally and cannot self-mint one, so this convenience path can't drive an s3 lane. "
    "The real store-mediated k8s loop is proven by test_e2e_lane + scripts/store-backend-canary.py."
)
def test_k8s_run_roundtrips(tmp_path):
    sb = Sandbox(backend="k8s")
    out = sb.run(["bash", "-lc", "echo hi > out.txt && echo done"],
                 workspace=str(tmp_path), output_paths=["out.txt"])
    assert out.exit_code == 0, out
    assert any(p.endswith("out.txt") for p in out.artifacts)
    assert (tmp_path / "out.txt").read_text().strip() == "hi"
    assert "done" in out.output, out
