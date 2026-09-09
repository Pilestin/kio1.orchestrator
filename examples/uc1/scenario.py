"""The four Use-Case 1 scenarios, as D2.6 specifies their sequences.

Each plan is the sequence written in the deliverable's use-case table, in the
same order and with the same KIOs. They are written down rather than asked of
a model on purpose: the point of this example is to exercise KIO1's dispatch,
gates, failover and selection end to end, and a run that only works when a
model happens to be installed and happens to plan correctly proves nothing
about any of those. `run.py --plan-with-model` asks the configured provider
instead, for when the planning itself is what you want to watch.
"""

from typing import Any

CUSTOMER_REQUEST = (
    "Our customers keep asking for bulk pricing. When someone orders 5 or more "
    "of the same item they should get 5% off that line, and at 10 or more it "
    "should be 10%. The order total has to be right even when the cart is "
    "empty, and it needs to come back fast enough for the checkout page."
)


def uc1_01() -> dict[str, Any]:
    """Natural-language customer request to draft issue and requirements."""
    return {
        "workflow_id": "wf-uc1-01",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO3",
                "capability": "requirements_engineering",
                "task": CUSTOMER_REQUEST,
            },
            {
                "step_id": "s2",
                "agent_id": "KIO3",
                "capability": "acceptance_criteria",
                "task": "Write acceptance criteria for the approved issue",
                "depends_on": ["s1"],
            },
        ],
        "explanation": "UC-UC1-01: a customer request becomes a structured issue.",
    }


def uc1_02() -> dict[str, Any]:
    """Automated code and test generation from the approved issue."""
    return {
        "workflow_id": "wf-uc1-02",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO4",
                "capability": "architecture_design",
                "task": "Synthesise the modules this issue needs",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO7",
                "capability": "code_generation",
                "task": "Generate the pricing module and its acceptance tests",
                "depends_on": ["s1"],
            },
            {
                "step_id": "s3",
                "agent_id": "KIO6",
                "capability": "data_validation",
                "task": "Validate that every requirement is traceable to the code",
                "depends_on": ["s2"],
            },
            {
                "step_id": "s4",
                "agent_id": "KIO12",
                "capability": "cybersecurity_validation",
                "task": "Scan the generated source",
                "depends_on": ["s2"],
            },
            {
                "step_id": "s5",
                "agent_id": "KIO9",
                "capability": "compliance_review",
                "task": "Check GDPR and responsible-AI compliance",
                "depends_on": ["s2"],
            },
        ],
        "explanation": "UC-UC1-02: the issue becomes code, tests, and checks.",
    }


def uc1_03() -> dict[str, Any]:
    """Automated bug localisation, fix suggestion, and verification."""
    return {
        "workflow_id": "wf-uc1-03",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO11",
                "capability": "test_automation",
                "task": "Run the suite and confirm the failure",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Localise the fault from the failing test",
                "depends_on": ["s1"],
            },
            {
                "step_id": "s3",
                "agent_id": "KIO2",
                "capability": "diagnosis",
                "task": "Explain the root cause",
                "depends_on": ["s2"],
            },
            {
                "step_id": "s4",
                "agent_id": "KIO7",
                "capability": "fix_generation",
                "task": "Propose and apply the patch",
                "depends_on": ["s3"],
            },
            {
                "step_id": "s5",
                "agent_id": "KIO11",
                "capability": "test_automation",
                "task": "Re-run the suite to verify the fix",
                "depends_on": ["s4"],
            },
            {
                "step_id": "s6",
                "agent_id": "KIO10",
                "capability": "energy_efficiency",
                "task": "Measure the cost of the fixed implementation",
                "depends_on": ["s5"],
            },
        ],
        "explanation": "UC-UC1-03: a real failure is located, fixed, and verified.",
    }


def uc1_04_refused() -> dict[str, Any]:
    """The same release, attempted while the suite is still red.

    UC-UC1-02 leaves a genuine failing test behind, and this is what should
    happen to anyone who tries to ship on top of it. It is the same plan as
    `uc1_04`; only the moment differs.
    """
    plan = uc1_04()
    plan["workflow_id"] = "wf-uc1-04a"
    plan["explanation"] = "A release attempt the gate should refuse."

    return plan


def uc1_04() -> dict[str, Any]:
    """Full end-to-end feature delivery with compliance and energy check."""
    return {
        "workflow_id": "wf-uc1-04",
        "execution_mode": "mixed",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO12",
                "capability": "cybersecurity_validation",
                "task": "Final vulnerability scan",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO9",
                "capability": "compliance_review",
                "task": "Final GDPR and responsible-AI validation",
            },
            {
                "step_id": "s3",
                "agent_id": "KIO11",
                "capability": "test_automation",
                "task": "Confirm the suite is green before release",
            },
            {
                "step_id": "s4",
                "agent_id": "KIO10",
                "capability": "energy_efficiency",
                "task": "Assess energy usage of the release build",
            },
            {
                "step_id": "s5",
                "agent_id": "KIO8",
                "capability": "packaging",
                "task": "Prepare the build",
                "depends_on": ["s3"],
            },
            {
                "step_id": "s6",
                "agent_id": "KIO8",
                "capability": "deployment",
                "task": "Release to staging and store the audit trail",
                "depends_on": ["s5"],
            },
        ],
        "explanation": "UC-UC1-04: the whole chain, gated on the checks passing.",
    }


# What each scenario has to be true of the world afterwards. A run that only
# prints a table proves nothing; these are what make it a test.
EXPECTATIONS: dict[str, dict[str, Any]] = {
    "wf-uc1-01": {
        "all_steps_ok": True,
        "files_present": [],
        "note": "Requirements and acceptance criteria produced.",
    },
    "wf-uc1-02": {
        "all_steps_ok": True,
        "files_present": ["pricing.py", "test_pricing.py"],
        "note": "Code and tests written to the workspace.",
    },
    "wf-uc1-04a": {
        "all_steps_ok": False,
        "files_present": [],
        "blocked_steps": ["s6"],
        "verdicts": {"s3": "defect"},
        "note": "The gate refused the release while the suite was red.",
    },
    "wf-uc1-03": {
        "all_steps_ok": True,
        "files_present": ["pricing.py"],
        "verdicts": {"s1": "defect", "s5": "clean"},
        "note": "The suite failed, was diagnosed and fixed, and then passed.",
    },
    "wf-uc1-04": {
        "all_steps_ok": True,
        "files_present": [],
        "note": "Every gate cleared and the build reached staging.",
    },
}

SCENARIOS = (
    ("UC-UC1-01", "Customer request to draft issue", uc1_01),
    ("UC-UC1-02", "Code and test generation", uc1_02),
    ("  (refused)", "Release attempt on a red suite", uc1_04_refused),
    ("UC-UC1-03", "Bug localisation, fix, verification", uc1_03),
    ("UC-UC1-04", "End-to-end delivery with gates", uc1_04),
)
