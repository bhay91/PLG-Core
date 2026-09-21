from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_modern_theme_is_loaded_last_and_applied_before_render():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.index("polish.css") < base.index("pps_modern.css") < base.index("pps_erp.css")
    assert "document.documentElement.dataset.theme" in base
    assert "localStorage.getItem('pps-theme')" in base
    assert "localStorage.setItem('pps-theme',next)" in base
    assert 'class="theme-toggle"' in base


def test_modern_theme_defines_shared_light_and_dark_design_tokens():
    css = (ROOT / "static" / "pps_modern.css").read_text(encoding="utf-8")
    assert ':root[data-theme="light"]' in css
    assert ':root[data-theme="dark"]' in css
    for token in (
        "--app-bg", "--sidebar-bg", "--topbar-bg", "--surface-1",
        "--border", "--text-primary", "--accent-blue", "--success",
        "--warning", "--danger", "--purple", "--teal",
    ):
        assert token in css


def test_modern_theme_keeps_responsive_shell_contract():
    css = (ROOT / "static" / "pps_modern.css").read_text(encoding="utf-8")
    assert "@media (max-width: 1100px)" in css
    assert "@media (max-width: 760px)" in css
    assert ".sidebar.mobile-open" in css
    assert ".main-shell" in css
