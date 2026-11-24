"""Browser smoke test: drives the remediation workflow through the UI only (no API shortcuts).

Prerequisites: the full stack running locally (see README "Run locally"), Google Chrome, Playwright
(`pip install playwright`), and a fresh project + defective run created by `python scripts/load_demo.py`,
which writes the ids to /tmp/ui_ids.txt.

    python scripts/ui_smoke.py
"""
from playwright.sync_api import sync_playwright, expect
import os
SHOTS = os.environ.get("SHOTS_DIR", "var/shots")
os.makedirs(SHOTS, exist_ok=True)
pid, rid = open("/tmp/ui_ids.txt").read().split()
B = "http://localhost:3000"
errors = []

def login(b, who):
    ctx = b.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(f"{who} pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"{who} console: {m.text}") if m.type == "error" else None)
    pg.goto(f"{B}/login"); pg.fill("input[type=email]", f"{who}@tanjung.example"); pg.fill("input[type=password]", "ModelAssure!2026")
    pg.click("button[type=submit]"); pg.wait_for_url(f"{B}/")
    return pg

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    # 1. Engineer opens evidence, then moves the three critical findings to READY_FOR_RETEST
    eng = login(b, "engineer")
    eng.goto(f"{B}/projects/{pid}/runs/{rid}?"); eng.wait_for_load_state("networkidle")
    eng.select_option("select[aria-label='Violated rule']", "POL-001"); eng.wait_for_timeout(800)
    eng.get_by_role("button", name="Evidence").first.click()
    expect(eng.get_by_text("evidence integrity verified")).to_be_visible(timeout=10000)
    eng.wait_for_timeout(500); eng.screenshot(path=SHOTS + "/evidence.png", full_page=False)
    eng.keyboard.press("Escape")
    eng.goto(f"{B}/projects/{pid}/findings"); eng.wait_for_load_state("networkidle")
    critical = eng.locator("li button", has_text="CRITICAL")
    n_critical = critical.count()
    assert n_critical == 3, f"expected 3 critical findings, got {n_critical}"
    for i in range(n_critical):
        critical.nth(i).click(); eng.wait_for_timeout(600)
        for label in ("Move to investigating", "Move to fix proposed", "Move to ready for retest"):
            eng.get_by_role("button", name=label).click(); eng.wait_for_timeout(700)
        expect(critical.nth(i)).to_contain_text("READY FOR RETEST")
    # 2. Retest via the button and modal, against 3.2.1
    eng.get_by_role("button", name="Retest 3 ready findings").click()
    dialog = eng.get_by_role("dialog")
    dialog.locator("select").first.select_option(label="3.2.1")
    dialog.get_by_role("button", name="Queue retest").click()
    eng.wait_for_url("**/runs/*"); expect(eng.get_by_text("COMPLETED").first).to_be_visible(timeout=60000)
    eng.wait_for_timeout(1500); eng.screenshot(path=SHOTS + "/retest-run.png", full_page=True)
    eng.goto(f"{B}/projects/{pid}/findings?run={rid}"); eng.wait_for_load_state("networkidle"); eng.wait_for_timeout(800)
    expect(eng.locator("li button", has_text="RESOLVED")).to_have_count(3, timeout=15000)
    # 3. Analyst queues a full run on 3.2.1 via the modal
    an = login(b, "analyst")
    an.goto(f"{B}/projects/{pid}/runs"); an.wait_for_load_state("networkidle")
    an.get_by_role("button", name="Queue validation run").click()
    d = an.get_by_role("dialog"); an.wait_for_timeout(800)
    d.locator("select").nth(2).select_option(label="3.2.1")
    d.get_by_role("button", name="Queue run").click()
    an.wait_for_url("**/runs/*"); expect(an.get_by_text("PASS").first).to_be_visible(timeout=90000)
    full_url = an.url
    # 4. Reviewer approves and generates the report
    rv = login(b, "reviewer")
    run_id = full_url.rsplit("/", 1)[1]
    rv.goto(f"{B}/projects/{pid}/release?run={run_id}"); rv.wait_for_load_state("networkidle"); rv.wait_for_timeout(1000)
    rv.fill("textarea", "Critical findings fixed in 3.2.1 and retested; unexpected DECLINE→REFER changes reviewed with Credit Risk.")
    rv.get_by_role("button", name="Record decision").click(); rv.wait_for_timeout(1200)
    expect(rv.get_by_text("APPROVE RELEASE").first).to_be_visible()
    rv.get_by_role("button", name="Generate new version").click()
    expect(rv.get_by_text("Version 1")).to_be_visible(timeout=20000)
    with rv.expect_download() as dl:
        rv.get_by_role("button", name="PDF").click()
    path = dl.value.path(); data = open(path, "rb").read(5)
    assert data == b"%PDF-", data
    rv.screenshot(path=SHOTS + "/release-approved.png", full_page=True)
    # 5. Reviewer tries to approve the failed run: must be refused with reasons shown
    rv.goto(f"{B}/projects/{pid}/release?run={rid}"); rv.wait_for_load_state("networkidle"); rv.wait_for_timeout(800)
    rv.fill("textarea", "Attempting to approve the defective build."); rv.get_by_role("button", name="Record decision").click()
    expect(rv.get_by_role("alert").filter(has_text="cannot be approved")).to_be_visible(timeout=5000)
    b.close()
print("UI FLOW OK")
print("\n".join(e for e in errors if "409" not in e) or "no browser errors")
