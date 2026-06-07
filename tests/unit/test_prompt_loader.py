
import pytest

from investment_agent.strategies.prompt_loader import PromptLoader


def test_render_with_variables(tmp_path):
    tmpl_dir = tmp_path / "prompts"
    tmpl_dir.mkdir()
    (tmpl_dir / "scoring.j2").write_text(
        "Analyze {{ ticker }} with PE={{ pe }}."
    )
    loader = PromptLoader(tmpl_dir)
    out = loader.render("scoring", ticker="AAPL", pe=28.5)
    assert out == "Analyze AAPL with PE=28.5."


def test_render_missing_template_raises(tmp_path):
    loader = PromptLoader(tmp_path)
    with pytest.raises(FileNotFoundError, match="ghost"):
        loader.render("ghost", ticker="X")


def test_render_supports_dicts_and_lists(tmp_path):
    tmpl_dir = tmp_path / "p"
    tmpl_dir.mkdir()
    (tmpl_dir / "deep.j2").write_text(
        "Peers:\n{% for p in peers %}- {{ p.ticker }}: PE={{ p.pe }}\n{% endfor %}"
    )
    loader = PromptLoader(tmpl_dir)
    out = loader.render(
        "deep",
        peers=[{"ticker": "MSFT", "pe": 32}, {"ticker": "GOOG", "pe": 25}],
    )
    assert "MSFT: PE=32" in out
    assert "GOOG: PE=25" in out
