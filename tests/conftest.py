import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real cache of the user."""
    monkeypatch.setenv("K_TRANSCRIPTOR_HOME", str(tmp_path / "home"))
