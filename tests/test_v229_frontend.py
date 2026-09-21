from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_version_is_2290():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "2.29.0"


def test_loading_ui_is_count_independent():
    source = (ROOT / "static/index.html").read_text(encoding="utf-8")
    loading = source[source.index("function loading") : source.index("function loading") + 500]
    assert ".repeat(" not in loading
    assert "正在載入全部機位資料" in loading
