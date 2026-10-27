"""Tests for the console's Markdown renderer (static/js/markdown.js).

The properties that matter: what models write in answers — lists, tables,
code, emphasis, links — becomes readable markup; nothing a model writes can
become markup it did not ask for, or a link that is not http(s); and code is
left exactly as written. Run through Node, the way the browser would load the
module; skipped where Node is not installed.
"""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

# As a data: address, so Node loads it as the ES module a browser sees; from
# its path, with no package.json, Node would take it for CommonJS.
MODULE = "data:text/javascript;base64," + base64.b64encode(
    (Path(__file__).resolve().parents[1] / "static" / "js" / "markdown.js").read_bytes()
).decode("ascii")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is not installed"
)


def render(text: str, *, sources: bool = False) -> str:
    """Return the module's markup for a piece of Markdown."""
    citations = (
        "{ citations: (html) => html.replace(/\\[(\\w+)\\]/g, '<s>$1</s>') }"
        if sources
        else "{}"
    )
    script = (
        f"import {{ renderMarkdown }} from {json.dumps(MODULE)};"
        f"process.stdout.write(JSON.stringify(renderMarkdown({json.dumps(text)}, {citations})));"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        check=True,
        timeout=30,
    )

    return json.loads(completed.stdout.decode("utf-8"))


def test_a_bulleted_list_becomes_a_list() -> None:
    assert render("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"


def test_a_numbered_list_stays_numbered() -> None:
    assert render("1. first\n2. second").startswith("<ol><li>first</li>")


def test_an_indented_item_nests_under_the_one_above() -> None:
    assert (
        render("- a\n  - b\n- c") == "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>"
    )


def test_a_table_becomes_a_table() -> None:
    html = render("| Name | Age |\n|---|---|\n| Ada | 36 |")

    assert "<th>Name</th><th>Age</th>" in html
    assert "<td>Ada</td><td>36</td>" in html


def test_a_code_block_is_kept_exactly_as_written() -> None:
    html = render("```python\ndef f(x):\n    return x * 2  # **not bold**\n```")

    assert 'data-lang="python"' in html
    assert "    return x * 2  # **not bold**" in html
    assert "<strong>" not in html


def test_markup_a_model_writes_is_shown_not_run() -> None:
    html = render("<script>alert(1)</script> and <b>bold</b>")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_only_web_addresses_become_links() -> None:
    assert '<a href="https://example.com"' in render("[docs](https://example.com)")
    assert "<a" not in render("[click](javascript:alert(1))")


def test_snake_case_is_not_read_as_emphasis() -> None:
    assert "<em>" not in render("call lookup_account_id now")
    assert "<em>this</em>" in render("only *this* is")


def test_bold_and_inline_code_are_marked() -> None:
    assert (
        render("**Python** `3.11`")
        == "<p><strong>Python</strong> <code>3.11</code></p>"
    )


def test_lines_of_one_paragraph_keep_their_breaks() -> None:
    assert render("first line\nsecond line") == "<p>first line<br>second line</p>"


def test_a_heading_is_a_section_not_a_page_title() -> None:
    assert render("# Summary") == "<h3>Summary</h3>"


def test_sources_are_marked_in_prose_but_not_in_code() -> None:
    html = render("Installed [Python], type `list[float]`", sources=True)

    assert "<s>Python</s>" in html
    assert "<code>list[float]</code>" in html
