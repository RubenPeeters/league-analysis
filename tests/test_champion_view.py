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
        "global.window = { addEventListener() {} };"
        "global.document = { addEventListener() {}, createElement(tag) { const e = {"
        " tag, dataset: {}, children: [], classes: new Set(),"
        " classList: { add(c) { e.classes.add(c); } },"
        " appendChild(c) { e.children.push(c); return c; }, append(...c) { e.children.push(...c); } };"
        " return e; } };"
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


def test_exact_rune_selection_flags_its_own_small_sample():
    # The page has 41 games but its exact selection only 7, so the selection is dimmed and warned.
    out = run(
        "(() => { const c = { children: [], appendChild(x) { this.children.push(x); } };"
        " m.renderRunes(c, { rune_games: 50, runes: [{ keystone: 1, primary_style: 2,"
        " secondary_style: 3, games: 41, share: 82, win_rate: 50, selection: { games: 7,"
        " primary: [11, 12], secondary: [13], shards: [5008] } }] });"
        " const all = []; const walk = e => { all.push(e); (e.children || []).forEach(walk); };"
        " walk(c); return all.filter(e => e.dataset && e.dataset.tip).map(e =>"
        " ({ cls: e.className, low: e.classes.has('cv-low'), warn: e.dataset.tip.includes('Small sample: 7') })); })()"
    )
    selection = [t for t in out if t["cls"] in ("cv-rune", "cv-shards")]
    assert len(selection) == 4 and all(t["low"] and t["warn"] for t in selection)
    figure = next(t for t in out if t["cls"] == "cv-figure")
    assert not figure["low"] and not figure["warn"]
