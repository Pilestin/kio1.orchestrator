"""Tests for UC-UC1-01/02's chain: KIO3 specifies, KIO4 designs, KIO6 checks.

The properties that matter: KIO3 turns a request into requirements a person
can trace back to the words, puts criteria on both sides of a threshold, and
asks about a vague word instead of guessing; KIO4 gives every requirement a
component and every component its reason, and reads the architecture of
code that exists; KIO6 finds where a requirement lives, tells a stub from an
implementation, and names the criterion the code contradicts or no test
checks.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from kio_mocks.kio3 import KIO as KIO3
from kio_mocks.kio4 import KIO as KIO4
from kio_mocks.kio6 import KIO as KIO6
from kio_mocks.kio7 import KIO as KIO7

SAMPLE = Path(__file__).resolve().parents[1] / "sample"

DISCOUNTS = "Customers want bulk pricing discounts: 5 or more items get 5%, 10 or more items get 10%."


def ask(
    kio: Any,
    capability: str,
    task: str = "Do it",
    upstream: dict[str, Any] | None = None,
    project: Path | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"upstream": upstream or {}}
    if project:
        data["repository"] = {"path": str(project)}
    reply = kio.execute(
        {
            "workflow_id": "wf-test",
            "step_id": "s1",
            "capability": capability,
            "task": task,
            "data": data,
        }
    )
    assert reply["status"] == "ok", reply["error"]
    return reply["output"]


def specify(task: str) -> dict[str, Any]:
    return ask(KIO3, "requirements_engineering", task)


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    copy = tmp_path / "shop-project"
    shutil.copytree(SAMPLE, copy)
    return copy


@pytest.fixture(scope="module")
def checked_sample(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    copy = tmp_path_factory.mktemp("shared") / "shop-project"
    shutil.copytree(SAMPLE, copy)
    return ask(KIO6, "quality_analysis", DISCOUNTS, project=copy)


# ── KIO3 ─────────────────────────────────────────────────────────────────────


def test_kio3_turns_each_item_of_a_list_into_its_own_requirement() -> None:
    output = specify(
        "Define requirements for a project management SaaS including user "
        "authentication, task boards, and real-time notifications"
    )

    assert [item["text"] for item in output["requirements"]] == [
        "The system shall support user authentication",
        "The system shall support task boards",
        "The system shall support real-time notifications",
    ]


def test_kio3_puts_criteria_on_both_sides_of_a_threshold() -> None:
    output = specify(DISCOUNTS)

    boundaries = [
        (item["given"], item["then"])
        for item in output["acceptance_criteria"]
        if item["when"] == "the total is calculated"
    ]
    assert boundaries == [
        ("5 items", "a 5% discount applies"),
        ("4 items", "the 5% discount does not apply"),
        ("10 items", "a 10% discount applies"),
        ("9 items", "the 10% discount does not apply"),
    ]


def test_kio3_asks_about_a_vague_word_instead_of_guessing() -> None:
    output = specify("The checkout must be fast.")

    assert output["nonfunctional_requirements"][0]["category"] == "performance"
    assert "“fast”" in output["ambiguities"][0]
    assert output["hitl_required"] is True
    assert output["verdict"] == "inconclusive"


def test_kio3_lets_a_person_do_an_action() -> None:
    output = specify("Admins should be able to export orders as CSV.")

    assert (
        output["requirements"][0]["text"]
        == "The system shall let the administrator export orders as CSV"
    )
    assert output["user_stories"][0]["text"].startswith(
        "As an administrator, I want to export orders as CSV"
    )


def test_kio3_keeps_a_rule_with_its_own_subject() -> None:
    output = specify("Invoices must include VAT.")

    assert output["requirements"][0]["text"] == "Invoices shall include VAT"
    assert output["acceptance_criteria"][0]["then"] == "invoices include VAT"


def test_kio3_reads_a_threshold_written_the_turkish_way() -> None:
    output = specify(
        "Müşteriler toplu alımda indirim istiyor: 5 ve üzeri ürün alana %5 indirim."
    )

    assert ("5 ürün", "a 5% discount applies") in [
        (item["given"], item["then"]) for item in output["acceptance_criteria"]
    ]


def test_kio3_reads_a_threshold_written_rate_first() -> None:
    output = specify(
        "Customers receive 5% discount when purchasing 5 or more items, "
        "and 10% discount when purchasing 10 or more items."
    )

    assert output["requirements"][0]["text"].startswith(
        "Customers shall receive 5% discount"
    )
    assert ("10 items", "a 10% discount applies") in [
        (item["given"], item["then"]) for item in output["acceptance_criteria"]
    ]


def test_kio3_keeps_a_who_phrase_with_its_subject() -> None:
    output = specify("A customer buying 5 or more items gets a 5% discount.")

    assert output["requirements"][0]["text"] == (
        "A customer buying 5 or more items shall get a 5% discount"
    )


def test_kio3_leaves_out_what_the_planner_says_about_the_document() -> None:
    output = specify(
        "Write natural-language requirements for a bulk discount system: customers receive "
        "5% discount when purchasing 5 or more items. Include acceptance criteria in the "
        "format: 'If item count >= 5, apply 5% discount.'"
    )

    assert len(output["requirements"]) == 1
    assert output["issue"]["title"] == "Bulk discount system"


def test_kio3_writes_the_specification_as_markdown() -> None:
    output = specify(DISCOUNTS)

    assert output["document"].startswith("# Customers want bulk pricing discounts\n")
    assert "## Acceptance criteria" in output["document"]


def test_kio3_refuses_an_empty_request() -> None:
    reply = KIO3.execute(
        {
            "workflow_id": "w",
            "step_id": "s1",
            "capability": "requirements_engineering",
            "task": "",
        }
    )

    assert reply["status"] == "error"
    assert "no request" in reply["error"]


# ── KIO4 ─────────────────────────────────────────────────────────────────────


def design(task: str) -> dict[str, Any]:
    return ask(KIO4, "architecture_design", upstream={"s1": specify(task)})


def test_kio4_gives_each_concept_its_own_service() -> None:
    output = design(
        "Users should be able to export reports as PDF. Invoices must include VAT."
    )

    services = {
        item["name"]: item["interfaces"]
        for item in output["components"]
        if item["kind"] == "service"
    }
    assert services == {
        "ReportService": ["export_report(request)"],
        "InvoiceService": ["validate_invoice(invoice)"],
    }


def test_kio4_traces_every_requirement_both_ways() -> None:
    output = design(
        "Users should be able to export reports as PDF. Invoices must include VAT."
    )

    trace = output["traceability"]
    assert trace["requirement_to_components"]["FR-01"] == ["Api", "ReportService"]
    assert trace["component_to_requirements"]["InvoiceService"] == ["FR-02"]
    assert trace["untraced_requirements"] == []
    assert trace["supporting_components"] == ["Store"]


def test_kio4_adds_a_component_for_each_quality_asked_for() -> None:
    output = design("Users should be able to export reports. Login must be secure.")

    guard = next(
        item for item in output["components"] if item["name"] == "AccessControl"
    )
    assert guard["requirements"] == ["NFR-01"]
    assert (
        "AccessControl"
        in next(item for item in output["components"] if item["name"] == "Api")[
            "depends_on"
        ]
    )


def test_kio4_says_why_a_rule_lives_in_its_service() -> None:
    output = design("Invoices must include VAT.")

    assert any(
        "InvoiceService" in item["decision"] and item["requirements"] == ["FR-01"]
        for item in output["decisions"]
    )


def test_kio4_writes_contracts_from_the_acceptance_criteria() -> None:
    output = ask(KIO4, "component_design", upstream={"s1": design(DISCOUNTS)})

    contract = next(
        item for item in output["contracts"] if item["operation"] == "get_discount"
    )
    assert any("given 5 items" in line for line in contract["postconditions"])


def test_kio4_recovers_the_modules_and_imports_of_existing_code(shop: Path) -> None:
    output = ask(
        KIO4,
        "architecture_design",
        "Recover the architecture of the existing code",
        project=shop,
    )

    pricing = next(
        item for item in output["components"] if item["name"] == "shop.pricing"
    )
    assert pricing["interfaces"] == [
        "discount_for(quantity)",
        "order_total(items)",
        "average_item_price(items)",
    ]
    assert output["cycles"] == []


def test_kio4_names_an_import_cycle(tmp_path: Path) -> None:
    (tmp_path / "orders.py").write_text("import billing\n", encoding="utf-8")
    (tmp_path / "billing.py").write_text("import orders\n", encoding="utf-8")

    output = ask(
        KIO4,
        "architecture_design",
        "Recover the architecture of the existing code",
        project=tmp_path,
    )

    assert sorted(output["cycles"][0][:2]) == ["billing", "orders"]
    assert output["verdict"] == "defect"


# ── KIO6 ─────────────────────────────────────────────────────────────────────


def test_kio6_finds_the_functions_a_requirement_lives_in(
    checked_sample: dict[str, Any],
) -> None:
    row = checked_sample["alignment"][0]

    assert row["where"][0] == "shop/pricing.py:6 discount_for"
    assert row["status"] == "implemented"


def test_kio6_names_the_boundary_no_test_checks(checked_sample: dict[str, Any]) -> None:
    checks = {item["id"]: item for item in checked_sample["alignment"][0]["criteria"]}

    assert checks["AC-02"]["status"] == "holds and tested"
    assert checks["AC-03"]["status"] == "holds, untested"
    assert "4" in checks["AC-03"]["detail"]


def test_kio6_reports_a_threshold_the_code_contradicts(shop: Path) -> None:
    pricing = shop / "shop" / "pricing.py"
    pricing.write_text(
        pricing.read_text(encoding="utf-8").replace("(5, 0.05)", "(6, 0.05)"),
        encoding="utf-8",
    )

    output = ask(KIO6, "quality_analysis", DISCOUNTS, project=shop)

    checks = {item["id"]: item["status"] for item in output["alignment"][0]["criteria"]}
    assert checks["AC-02"] == "numbers not in the code"
    assert output["verdict"] == "defect"


def test_kio6_finds_no_place_for_what_the_code_lacks(shop: Path) -> None:
    output = ask(
        KIO6, "quality_analysis", "Users want to export invoices as PDF.", project=shop
    )

    assert output["alignment"][0]["status"] == "missing"
    assert output["verdict"] == "defect"


def test_kio6_tells_a_skeleton_from_an_implementation() -> None:
    designed = design(DISCOUNTS)
    skeleton = ask(KIO7, "code_generation", upstream={"s1": designed})

    output = ask(KIO6, "quality_analysis", upstream={"s2": skeleton})

    row = output["alignment"][0]
    assert row["where"] == [
        "api.py:7 handle_get_discount",
        "discount_service.py:7 get_discount",
    ]
    assert row["status"] == "stub"
    assert {item["status"] for item in row["criteria"]} == {"not written yet"}
    assert output["verdict"] == "inconclusive"


def test_kio6_says_data_validation_is_not_written_yet() -> None:
    reply = KIO6.execute(
        {
            "workflow_id": "w",
            "step_id": "s1",
            "capability": "data_validation",
            "task": "x",
        }
    )

    assert reply["status"] == "error"
    assert "step 5" in reply["error"]
