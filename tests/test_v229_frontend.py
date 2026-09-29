from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_version_is_at_least_2291():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert tuple(map(int, version.split("."))) >= (2, 29, 1)


def test_loading_ui_is_count_independent():
    source = (ROOT / "static/index.html").read_text(encoding="utf-8")
    loading = source[source.index("function loading") : source.index("function loading") + 500]
    assert ".repeat(" not in loading
    assert "正在載入全部機位資料" in loading
