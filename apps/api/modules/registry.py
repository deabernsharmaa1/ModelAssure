"""Routers added beyond the Phase 2 core."""


def extra_routers():
    from modules.policies.router import router as policies
    from modules.runner.router import router as runs
    from modules.scenarios.router import router as scenarios

    return [policies, scenarios, runs]
