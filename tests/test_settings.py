import pytest

from config import settings


def test_orders_always_disabled(monkeypatch):
    monkeypatch.setenv("ORDERS_ENABLED", "true")
    assert settings.load_settings().orders_enabled is False


def test_default_mode_is_mock(monkeypatch):
    monkeypatch.delenv("DATA_MODE", raising=False)
    s = settings.load_settings()
    assert s.data_mode == "MOCK"
    assert "PAPER MODE" in s.mode_label


def test_bad_mode_rejected(monkeypatch):
    monkeypatch.setenv("DATA_MODE", "BANANA")
    with pytest.raises(ValueError):
        settings.load_settings()
