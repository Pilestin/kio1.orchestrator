"""Tests for UC-UC1-03's chain: KIO11 confirms, KIO2 localises, KIO7 fixes, KIO10 weighs.

Run on a copy of `sample/`, whose `average_item_price([])` divides by zero.
The properties that matter: KIO11 finds the failing test, and finds the edge
on its own when no test covers it; KIO2 names the line that raises — not the
test where it surfaced — with the values and the slice that led there, and
never offers a fix; KIO7's patch makes every test pass on a copy and changes
the workspace only when told to; KIO10 measures what the fix costs.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from kio_mocks.kio2 import KIO as KIO2
from kio_mocks.kio7 import KIO as KIO7
from kio_mocks.kio10 import KIO as KIO10
from kio_mocks.kio11 import KIO as KIO11

SAMPLE = Path(__file__).resolve().parents[1] / "sample"


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    """A copy a test may change."""
    copy = tmp_path / "shop-project"
    shutil.copytree(SAMPLE, copy)
    return copy


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One copy for the tests that only read: every run of it costs a pytest."""
    copy = tmp_path_factory.mktemp("shared") / "shop-project"
    shutil.copytree(SAMPLE, copy)
    return copy


@pytest.fixture(scope="module")
def located(shared: Path) -> dict[str, Any]:
    return ask(KIO2, "bug_localization", shared)


@pytest.fixture(scope="module")
def diagnosed(shared: Path) -> dict[str, Any]:
    return ask(KIO2, "diagnosis", shared)


@pytest.fixture(autouse=True)
def one_energy_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kio_mocks.kio10.RUNS", 1)


def ask(
    kio: Any, capability: str, project: Path, task: str = "Do it", **data: Any
) -> dict[str, Any]:
    reply = kio.execute(
        {
            "workflow_id": "wf-test",
            "step_id": "s1",
            "capability": capability,
            "task": task,
            "data": {"repository": {"path": str(project)}, **data},
        }
    )
    assert reply["status"] == "ok", reply["error"]
    return reply["output"]


def test_kio11_reports_the_failing_test_and_where_it_broke(shared: Path) -> None:
    output = ask(KIO11, "test_automation", shared)

    assert output["passed"] == 4
    assert (
        output["failed"][0]["test"]
        == "tests/test_pricing.py::test_an_empty_cart_has_no_average_price"
    )
    assert output["failed"][0]["where"] == "shop/pricing.py:25"
    assert output["verdict"] == "defect"


def test_kio11_finds_the_edge_on_its_own_when_no_test_covers_it(shop: Path) -> None:
    (shop / "tests" / "test_pricing.py").unlink()

    output = ask(KIO11, "test_automation", shop, task="Generate edge-case tests")

    defects = output["generated"]["defects"]
    assert [item["error"] for item in defects] == ["ZeroDivisionError"]
    assert defects[0]["where"] == "shop/pricing.py:25"


def test_kio2_names_the_line_that_raises_not_the_test(located: dict[str, Any]) -> None:
    output = located

    first = output["findings"][0]
    assert (first["file"], first["line"]) == ("shop/pricing.py", 25)
    assert "total_quantity = 0" in output["summary"]
    assert output["trace_ref"].startswith("kio-mocks://trace/")


def test_kio2_slices_back_to_where_the_zero_came_from(located: dict[str, Any]) -> None:
    output = located

    lines = {(entry["file"], entry["line"]) for entry in output["slice_context"]}
    assert ("shop/pricing.py", 24) in lines  # total_quantity = sum(...)
    assert ("tests/test_pricing.py", 21) in lines  # average_item_price([])


def test_kio2_takes_the_failing_test_kio11_reported(shared: Path) -> None:
    reported = {
        "failed": [
            {"test": "tests/test_pricing.py::test_an_empty_cart_has_no_average_price"}
        ]
    }

    output = ask(KIO2, "diagnosis", shared, upstream={"s1": reported})

    assert "total_quantity is 0" in output["root_cause"]


def test_kio2_does_not_fix(shop: Path) -> None:
    reply = KIO2.execute(
        {
            "workflow_id": "w",
            "step_id": "s",
            "capability": "fix_recommendation",
            "task": "fix",
            "data": {},
        }
    )

    assert reply["status"] == "error"
    assert "KIO7" in reply["error"]


def test_kio2_on_a_passing_test_finds_nothing(shared: Path) -> None:
    output = ask(
        KIO2,
        "bug_localization",
        shared,
        arguments={"test": "tests/test_pricing.py::test_a_single_item_has_no_discount"},
    )

    assert output["verdict"] == "clean"
    assert output["findings"] == []


def test_kio2_replays_up_to_the_exception(
    shared: Path, located: dict[str, Any]
) -> None:
    output = ask(KIO2, "replay", shared, upstream={"s1": located})

    assert output["steps"][-1]["event"] in ("exception", "return")
    assert any(
        step.get("exception", "").startswith("ZeroDivisionError")
        for step in output["steps"]
    )


def test_kio2_aligns_a_failing_run_with_a_passing_one(
    shared: Path, located: dict[str, Any]
) -> None:
    output = ask(KIO2, "trace_alignment", shared, upstream={"s1": located})

    assert output["divergence"] is not None
    assert len(output["trace_refs"]) == 2


def test_kio7s_fix_passes_every_test_and_leaves_the_workspace_alone(
    shared: Path, diagnosed: dict[str, Any]
) -> None:
    before = (shared / "shop" / "pricing.py").read_text(encoding="utf-8")

    output = ask(KIO7, "code_generation", shared, upstream={"s1": diagnosed})

    assert "+    if not total_quantity:" in output["patch"]
    assert "+        return 0.0" in output["patch"]
    assert output["verified"]["failed"] == []
    assert output["applied"] is False
    assert (shared / "shop" / "pricing.py").read_text(encoding="utf-8") == before


def test_kio7_applies_the_fix_only_when_told(shop: Path) -> None:
    diagnosis = ask(KIO2, "diagnosis", shop)

    output = ask(
        KIO7,
        "code_generation",
        shop,
        upstream={"s1": diagnosis},
        arguments={"apply": True},
    )

    assert output["applied"] is True
    assert (shop / "shop" / "pricing.py.orig").is_file()
    assert ask(KIO11, "test_automation", shop)["failed"] == []


def test_kio7_points_at_the_unchecked_divisor(shared: Path) -> None:
    output = ask(KIO7, "code_understanding", shared, task="Explain shop/pricing.py")

    assert [(item["line"], item["risk"]) for item in output["suggestions"]] == [
        (25, "possible division by zero")
    ]


def test_kio7_writes_a_skeleton_from_an_architecture(shared: Path) -> None:
    components = [
        {
            "name": "Cart",
            "responsibility": "Holds items.",
            "interfaces": ["add_item(cart, item)"],
        }
    ]

    output = ask(
        KIO7, "code_generation", shared, upstream={"s1": {"components": components}}
    )

    paths = [file["path"] for file in output["files"]]
    assert paths == ["cart.py", "tests/test_cart.py", "conftest.py"]
    assert "def add_item(cart, item):" in output["files"][0]["content"]


def test_kio10_weighs_the_fix_against_the_code_before(
    shared: Path, diagnosed: dict[str, Any]
) -> None:
    fix = ask(KIO7, "code_generation", shared, upstream={"s1": diagnosed})

    output = ask(KIO10, "energy_efficiency", shared, upstream={"s2": fix})

    assert output["baseline"]["joules"] > 0
    assert "alternative" in output
    assert isinstance(output["change"], float)


def test_kio10_falls_back_to_a_board_the_model_fits(shared: Path) -> None:
    output = ask(
        KIO10,
        "tinyml",
        shared,
        arguments={"model_kb": 900, "device": "nRF52840", "macs": 2_000_000},
    )

    assert output["fallback_from"] == "nRF52840"
    assert output["device"] == "ESP32-S3"
    assert output["simulated"] is True
