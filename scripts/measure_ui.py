"""NFR-01 benchmark: p95 time until each principal screen has loaded its data (network idle),
measured in headless Chrome against a running local stack with the demo loaded."""
import statistics
import time

from playwright.sync_api import sync_playwright

pid, rid = open("/tmp/ui_ids.txt").read().split()
B = "http://localhost:3000"
PAGES = ["/", f"/projects/{pid}", f"/projects/{pid}/connections", f"/projects/{pid}/builder", f"/projects/{pid}/runs",
         f"/projects/{pid}/runs/{rid}", f"/projects/{pid}/findings", f"/projects/{pid}/release", "/audit"]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page()
    pg.goto(f"{B}/login"); pg.fill("input[type=email]", "risk@tanjung.example"); pg.fill("input[type=password]", "ModelAssure!2026")
    pg.click("button[type=submit]"); pg.wait_for_url(f"{B}/")
    times = []
    for _ in range(5):
        for path in PAGES:
            t0 = time.perf_counter(); pg.goto(B + path); pg.wait_for_load_state("networkidle"); times.append(time.perf_counter() - t0)
    b.close()
times.sort()
p95 = times[int(0.95 * (len(times) - 1))]
print(f"{len(times)} page loads: median {statistics.median(times):.2f}s, p95 {p95:.2f}s, max {times[-1]:.2f}s (target p95 < 2s)")
