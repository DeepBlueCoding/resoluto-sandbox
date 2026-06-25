"""Guards that no internal-only ticket/section cross-refs remain in shipped source."""
import re, pathlib
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

def test_no_internal_ticket_or_section_refs():
    pat = re.compile(r"RES-\d+|§\d")
    hits = [str(p) for p in SRC.rglob("*.py") if pat.search(p.read_text())]
    assert not hits, f"internal refs remain: {hits}"
