import pytest

_REDTEAM_KEY = pytest.StashKey[dict]()


def pytest_configure(config):
    config.stash[_REDTEAM_KEY] = {"layers": {}, "limits": None}


@pytest.fixture(scope="session")
def redteam_log(request):
    """Shared record of red-team outcomes, printed in the terminal summary."""
    return request.config.stash[_REDTEAM_KEY]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    log = config.stash.get(_REDTEAM_KEY, None)
    if not log or not log["layers"]:
        return
    tr = terminalreporter
    tr.section("SQL guard red-team summary")
    full = log["layers"].get("full_guard", {})
    blocked = sum(1 for ok in full.values() if ok)
    tr.write_line(f"{blocked} of {len(full)} red-team cases blocked")
    auth = log["layers"].get("text_check_bypassed", {})
    if auth:
        a_blocked = sum(1 for ok in auth.values() if ok)
        tr.write_line(
            f"(with the text check bypassed: {a_blocked} of {len(auth)} red-team "
            "cases blocked by the connection/authorizer layers alone)"
        )
    if log["limits"]:
        tr.write_line(log["limits"])
