from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_app_smoke() -> None:
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=20)
    assert not app.exception
    assert app.title[0].value == "Acme Invoice Automation"
    assert app.button[0].label == "Process invoice"
    assert [item.label for item in app.text_input] == ["xAI API key", "Model", "Base URL"]
    assert app.text_input[0].value == ""
    assert app.text_input[1].value == "grok-4.7"
