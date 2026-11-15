"""Tooltip helpers in ui/script.js, run through node with a stub document."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_JS = Path(__file__).resolve().parent.parent / "ui" / "script.js"


def run(expr):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    script = (
        "global.document = { addEventListener() {} };"
        f"const m = require({json.dumps(str(SCRIPT_JS))});"
        f"console.log(JSON.stringify({expr}));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_esc_escapes_markup():
    assert run("m.esc(`<img src=x onerror=\"a('b')\">&`)") == (
        "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;"
    )


def test_tip_escapes_data_dragon_text():
    html = run("m.tipHtml({ title: '<b>Item</b>', about: 'x<script>', lines: ['1 & 2'] })")
    assert "<b>" not in html and "<script>" not in html
    assert "&lt;b&gt;Item&lt;/b&gt;" in html and "1 &amp; 2" in html


def test_tip_flags_small_samples():
    assert run("m.LOW_SAMPLE") == 10
    assert "Small sample: 9 games." in run("m.tipHtml({ title: 't', games: 9 })")
    assert "Small sample: 1 game." in run("m.tipHtml({ title: 't', games: 1 })")
    assert "Small sample" not in run("m.tipHtml({ title: 't', games: 10 })")
