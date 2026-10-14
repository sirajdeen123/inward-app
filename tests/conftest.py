from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parents[1] / "Sample Input files"


def sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample file missing: {path}")
    return path


@pytest.fixture(scope="session")
def sit_import():
    from inward.importers.sit import parse_sit
    return parse_sit(sample("SIT.xlsx"))


@pytest.fixture(scope="session")
def irr_import():
    from inward.importers.irr import parse_irr
    return parse_irr(sample("IRR.xlsx"))


@pytest.fixture
def seeded(db):
    from datetime import date

    from inward.core.seed import seed_reference_data
    seed_reference_data(date(2026, 10, 19))


@pytest.fixture
def make_user(db):
    from inward.accounts.services import create_user

    def _make(username: str, role: str):
        return create_user(None, username=username, full_name=username.title(), email=f"{username}@example.com",
                           password="Correct-Horse-Battery-9", role_code=role)
    return _make
