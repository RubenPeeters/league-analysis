"""buildView in ui/history.js on a hand-written history, run through node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HISTORY_JS = Path(__file__).resolve().parent.parent / "ui" / "history.js"


def champ(name, pick):
    return {"name": name, "games": 1, "pick_rate": pick, "win_rate": 50.0, "ban_rate": 1.0}


def patch(name, top, complete=True):
    return {
        "patch": name,
        "complete": complete,
        "regions": {"kr": {"games": 10, "games_estimated": False, "roles": {"TOP": top}}},
    }


def build_view(history, region="kr", role="TOP"):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    script = (
        f"const {{ buildView }} = require({json.dumps(str(HISTORY_JS))});"
        f"console.log(JSON.stringify(buildView({json.dumps(history)}, "
        f"{json.dumps(region)}, {json.dumps(role)})));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_rows_columns_and_changes():
    # 16.10 is listed before 16.9 to check the numeric patch sort.
    top15 = [champ(f"C{i}", 20 - i) for i in range(15)]
    history = {
        "schema": 1,
        "patches": [
            patch("16.10", [*top15[:14], champ("New", 1.0), champ("Tail", 0.5)]),
            patch("16.9", top15, complete=False),
        ],
    }
    v = build_view(history)

    assert [p["patch"] for p in v["patches"]] == ["16.9", "16.10"]
    names = [r["name"] for r in v["rows"]]
    # Tail is listed but never in the top 15; C14 and New were in it once.
    assert "Tail" not in names
    assert names[:2] == ["C0", "C1"]
    assert set(names[-2:]) == {"C14", "New"}
    rows = {r["name"]: r for r in v["rows"]}
    assert rows["New"]["cells"][0] is None  # not listed in 16.9: empty, not 0
    assert rows["C14"]["cells"][1] is None
    assert v["maxPick"] == 20
    assert v["mostPicked"] == [{"patch": "16.9", "name": "C0"}, {"patch": "16.10", "name": "C0"}]
    assert v["entered"] == ["New"]
    assert v["left"] == ["C14"]


def test_missing_role_or_region_gives_empty_rows():
    v = build_view({"schema": 1, "patches": [patch("16.2", [champ("A", 5.0)])]}, region="euw1")
    assert v["rows"] == []
    assert v["mostPicked"] == [{"patch": "16.2", "name": None}]
    assert v["entered"] == [] and v["left"] == []
