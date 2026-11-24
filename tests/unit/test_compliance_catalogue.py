"""The regulatory catalogue is data: make sure it is well-formed and every evidence check exists."""
from modules.compliance.mapping import RunEvidence, load_frameworks


def test_frameworks_load_and_reference_known_checks():
    fws = load_frameworks()
    assert {"BNM-AI-FS-2026", "BNM-RMIT-2026", "MAS-AIRM-2026", "AICB-AI-MRM"} <= set(fws)
    seen = set()
    for fw in fws.values():
        for key in ("name", "issuer", "status", "nature", "source_url"):
            assert fw.get(key), (fw["framework_id"], key)
        for c in fw["controls"]:
            assert c["control_id"] not in seen
            seen.add(c["control_id"])
            assert c["max_coverage"] in ("FULL", "PARTIAL", "NONE")
            if c["max_coverage"] == "NONE":
                assert not c["checks"] and c.get("note"), c["control_id"]
            for chk in c["checks"]:
                assert hasattr(RunEvidence, f"_c_{chk}"), chk


def test_pending_framework_has_no_controls_yet():
    assert load_frameworks()["AICB-AI-MRM"]["controls"] == []
    assert load_frameworks()["AICB-AI-MRM"]["status"] == "PENDING_PUBLICATION"
