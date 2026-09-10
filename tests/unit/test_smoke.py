"""Smoke test: verify project package is importable."""


def test_import_settings():
    from etl_agent.settings import get_settings

    settings = get_settings()
    assert settings.env
    assert settings.api_v1_prefix


def test_import_main():
    from etl_agent.main import app

    assert app is not None
