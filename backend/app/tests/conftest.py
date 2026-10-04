"""Shared pytest setup: run every test against an isolated temp DATA_DIR."""
import os
import pytest

from app import seed


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    yield tmp_path
