import re
from figcite.webui import PAGE


def test_the_page_never_preselects_a_candidate():
    """Scoped to <input> tags on purpose. A blanket `"checked" not in PAGE`
    is unsatisfiable -- any JS that reads a radio says `el.checked` -- and an
    assertion that cannot pass gets deleted by the next person, taking the
    real guarantee with it."""
    for tag in re.findall(r"<input[^>]*>", PAGE):
        assert "checked" not in tag, f"pre-selected candidate: {tag}"


def test_the_page_renders_lookup_errors_distinctly_from_no_matches():
    assert "LOOKUP FAILED" in PAGE
    assert "item.error" in PAGE


def test_the_page_shows_why_a_doi_is_trusted():
    assert "doi_evidence" in PAGE


def test_the_page_is_self_contained():
    assert not re.search(r'src="https?://', PAGE), "no third-party assets"
