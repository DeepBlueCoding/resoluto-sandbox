"""Guards that the core import stays pydantic-only (no heavy backend deps)."""

import subprocess
import sys


def test_core_import_pulls_no_heavy_deps():
    code = (
        "import importlib, sys; importlib.import_module('resoluto.sandbox');"
        "bad=[m for m in ('kubernetes_asyncio','aioboto3','botocore','gcloud') "
        "if m in sys.modules];"
        "print('LEAK:'+','.join(bad)); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
