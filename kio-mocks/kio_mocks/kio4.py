"""KIO4 — architecture and component design.

D2.6 §4.1.4 and UC-UC1-01: from KIO3's requirements, the components and how
they depend on each other (FR-KIO4-01/02), the interfaces each one offers
(FR-KIO4-03), traceability both ways between requirements and components
(FR-KIO4-05), the reasons for each decision (FR-KIO4-06), a diagram
(FR-KIO4-04) and the skeleton KIO7 turns into code (FR-KIO4-08). From code
that already exists it recovers the architecture: modules, the import graph,
and its cycles (FR-KIO4-09).

Without a model the design follows one rule, stated in the output: a layered
service architecture with one service per thing the requirements are about
("export reports" and "reports by month" both belong to a report service),
an API in front, a store behind, and a component for each quality the
non-functional requirements ask for. Every component says which
requirements put it there; one that no requirement asked for says so.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.kio3 import UI, analyse
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.text import STOP, is_action, pascal, singular, snake, words

PREPOSITION = re.compile(
    r"[:;,(]|\s(?:as|of|with|by|for|in|on|to|from|per|into|at|when|that|which|so)\s",
    re.IGNORECASE,
)
SHALL = re.compile(
    r"^.*?\bshall\s+(?:let\s+the\s+\w+\s+|provide\s+|support\s+)?", re.IGNORECASE
)
ARTICLES = {"a", "an", "the", "their", "its", "our"}

# What each kind of quality adds to the design, and why.
QUALITIES = {
    "performance": (
        "Cache",
        "Keeps answers the services have just computed, so repeated reads skip the store.",
        ["get(key)", "put(key, value, ttl_seconds)"],
    ),
    "security": (
        "AccessControl",
        "Checks who is calling and what they may do before a request reaches a service.",
        ["authenticate(credentials)", "authorise(principal, action)"],
    ),
    "reliability": (
        "HealthMonitor",
        "Watches the services and the store and reports when one stops answering.",
        ["check()", "report(component, status)"],
    ),
    "scalability": (
        "JobQueue",
        "Takes slow work off the request path so more requests are served at once.",
        ["enqueue(job)", "next_job()"],
    ),
}


def _goal(item: dict[str, Any]) -> str:
    """What a requirement asks for, from KIO3's fields or from its sentence."""
    if item.get("goal"):
        return str(item["goal"])
    return SHALL.sub("", str(item.get("text", ""))).strip(" .")


def _head(item: dict[str, Any]) -> str:
    """The thing a requirement is about: "export reports as PDF" → report."""
    phrase = (
        str(item["subject"])
        if item.get("kind") == "rule" and item.get("subject")
        else _goal(item)
    )
    if is_action(phrase) and len(phrase.split()) > 1:
        phrase = phrase.split(None, 1)[1]
    phrase = PREPOSITION.split(" " + phrase + " ", 1)[0]
    found = [
        word for word in words(phrase) if word not in ARTICLES and word not in STOP
    ]
    if not found:
        return "core"
    return singular(found[-1])


def _interface(item: dict[str, Any], head: str) -> str:
    goal = _goal(item)
    name = snake(head)
    if item.get("kind") == "rule":
        return f"validate_{name}({name})"
    if is_action(goal):
        return f"{snake(goal.split()[0])}_{name}(request)"
    return f"get_{name}(context)"


def _requirements(request: Request) -> dict[str, Any]:
    """KIO3's output when it is upstream; otherwise KIO3's reading of the task."""
    functional = request.find_upstream("requirements")
    if isinstance(functional, list) and functional:
        nonfunctional = request.find_upstream("nonfunctional_requirements") or []
        criteria = request.find_upstream("acceptance_criteria") or []
        return {
            "requirements": [item for item in functional if isinstance(item, dict)],
            "nonfunctional_requirements": [
                item for item in nonfunctional if isinstance(item, dict)
            ],
            "acceptance_criteria": [
                item for item in criteria if isinstance(item, dict)
            ],
            "ambiguities": request.find_upstream("ambiguities") or [],
            "from": "upstream",
        }
    found = analyse(str(request.arguments.get("text") or request.task))
    return {**found, "from": "task"}


def design(found: dict[str, Any]) -> dict[str, Any]:
    """Components, dependencies, traceability and decisions for a set of requirements."""
    functional = found["requirements"]
    nonfunctional = found.get("nonfunctional_requirements") or []
    if not functional:
        raise KioError("There are no functional requirements to design for.")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in functional:
        groups[_head(item)].append(item)

    actors = sorted({str(item.get("actor") or "user") for item in functional})
    services: list[dict[str, Any]] = []
    for head, items in groups.items():
        interfaces = list(dict.fromkeys(_interface(item, head) for item in items))
        services.append(
            {
                "name": f"{pascal(head)}Service",
                "kind": "service",
                "responsibility": f"Owns {head} and the rules about it: "
                + "; ".join(_goal(item) for item in items)
                + ".",
                "interfaces": interfaces,
                "requirements": [item["id"] for item in items],
                "depends_on": ["Store"],
            }
        )

    qualities: list[dict[str, Any]] = []
    for category, (name, responsibility, interfaces) in QUALITIES.items():
        ids = [item["id"] for item in nonfunctional if item.get("category") == category]
        if ids:
            qualities.append(
                {
                    "name": name,
                    "kind": "cross-cutting",
                    "responsibility": responsibility,
                    "interfaces": interfaces,
                    "requirements": ids,
                    "depends_on": [],
                }
            )
    quality_names = {item["name"] for item in qualities}
    for service in services:
        service["depends_on"] += [
            name for name in ("Cache", "JobQueue") if name in quality_names
        ]

    api = {
        "name": "Api",
        "kind": "interface",
        "responsibility": f"Takes requests from the {' and '.join(actors)} and hands each to the service that owns it.",
        "interfaces": [
            f"handle_{interface.split('(')[0]}(request)"
            for service in services
            for interface in service["interfaces"]
        ],
        "requirements": [item["id"] for item in functional],
        "depends_on": [service["name"] for service in services]
        + [
            name for name in ("AccessControl", "HealthMonitor") if name in quality_names
        ],
    }
    components: list[dict[str, Any]] = [api, *services, *qualities]
    on_screen = [
        item
        for item in functional
        if UI.search(str(item.get("source") or item.get("text", "")))
    ]
    if on_screen:
        usability = [
            item["id"] for item in nonfunctional if item.get("category") == "usability"
        ]
        components.insert(
            0,
            {
                "name": "WebUi",
                "kind": "presentation",
                "responsibility": "The screens the requirements name, calling the API.",
                "interfaces": [
                    f"render_{snake(head)}_view(state)"
                    for head in dict.fromkeys(_head(item) for item in on_screen)
                ],
                "requirements": [item["id"] for item in on_screen] + usability,
                "depends_on": ["Api"],
            },
        )
    components.append(
        {
            "name": "Store",
            "kind": "data",
            "responsibility": "Keeps the records the services read and write.",
            "interfaces": ["load(kind, key)", "save(kind, record)"],
            "requirements": [],
            "depends_on": [],
        }
    )

    forward = {
        item["id"]: [c["name"] for c in components if item["id"] in c["requirements"]]
        for item in [*functional, *nonfunctional]
    }
    backward = {c["name"]: c["requirements"] for c in components}
    gaps = [requirement for requirement, names in forward.items() if not names]

    decisions = [
        {
            "decision": "A layered service architecture: an API in front, one service per concept, a store behind.",
            "because": f"The {len(functional)} functional requirements are about {len(groups)} "
            f"concept(s) ({', '.join(groups)}); each changes for its own reasons, so each gets its own service.",
            "requirements": [item["id"] for item in functional],
        }
    ]
    for item in functional:
        if item.get("kind") == "rule":
            owner = f"{pascal(_head(item))}Service"
            decisions.append(
                {
                    "decision": f"The rule “{item.get('text', '')}” lives in {owner}, not in the API.",
                    "because": "Every way into the system then obeys it.",
                    "requirements": [item["id"]],
                }
            )
    for quality in qualities:
        decisions.append(
            {
                "decision": f"Add {quality['name']}.",
                "because": quality["responsibility"],
                "requirements": quality["requirements"],
            }
        )
    if any(item.get("category") == "usability" for item in nonfunctional):
        decisions.append(
            {
                "decision": "Usability is a property of the screens, not a component.",
                "because": "It is checked in WebUi's reviews and tests.",
                "requirements": [
                    item["id"]
                    for item in nonfunctional
                    if item.get("category") == "usability"
                ],
            }
        )
    for index, decision in enumerate(decisions, 1):
        decision["id"] = f"AD-{index:02d}"

    return {
        "style": "layered (API → services → store)",
        "components": components,
        "traceability": {
            "requirement_to_components": forward,
            "component_to_requirements": backward,
            "untraced_requirements": gaps,
            "supporting_components": [
                c["name"] for c in components if not c["requirements"]
            ],
        },
        "decisions": decisions,
        "open_questions": list(found.get("ambiguities") or []),
        "diagram": _mermaid(components),
        "scaffold": [f"{snake_name(c['name'])}.py" for c in components]
        + [f"tests/test_{snake_name(c['name'])}.py" for c in components],
    }


def snake_name(name: str) -> str:
    """`ReportService` → `report_service`, as KIO7 names the module."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def _mermaid(components: list[dict[str, Any]]) -> str:
    lines = ["flowchart LR"]
    for component in components:
        ids = ", ".join(component["requirements"][:4]) + (
            "…" if len(component["requirements"]) > 4 else ""
        )
        label = component["name"] + (f"<br/><small>{ids}</small>" if ids else "")
        lines.append(f'  {component["name"]}["{label}"]')
    for component in components:
        for target in component["depends_on"]:
            lines.append(f"  {component['name']} --> {target}")
    return "\n".join(lines)


def markdown(found: dict[str, Any], designed: dict[str, Any]) -> str:
    out = [
        "# Architecture",
        "",
        f"Style: {designed['style']}.",
        "",
        "## Components",
        "",
    ]
    for component in designed["components"]:
        traced = ", ".join(component["requirements"]) or "supporting, no requirement"
        out.append(
            f"- **{component['name']}** ({component['kind']}, {traced}) — {component['responsibility']}"
        )
        out += [f"  - `{interface}`" for interface in component["interfaces"]]
    out += ["", "## Decisions", ""] + [
        f"- **{d['id']}** {d['decision']} {d['because']} ({', '.join(d['requirements'])})"
        for d in designed["decisions"]
    ]
    if designed["open_questions"]:
        out += ["", "## To confirm", ""] + [
            f"- {item}" for item in designed["open_questions"]
        ]
    out += ["", "## Diagram", "", "```mermaid", designed["diagram"], "```"]
    return "\n".join(out) + "\n"


# ── recovery from existing code ─────────────────────────────────────────────


def recover(request: Request) -> dict[str, Any]:
    """The architecture a codebase already has: its modules and what imports what."""
    root = request.workspace.require()
    modules: dict[str, dict[str, Any]] = {}

    for path in request.workspace.python_files():
        relative = request.workspace.relative(path)
        if (
            relative.startswith("tests/")
            or "/tests/" in relative
            or path.name.startswith("test_")
        ):
            continue
        name = relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                base = node.module
                if node.level:
                    package = (
                        name.split(".")[: -node.level]
                        if path.name != "__init__.py"
                        else name.split(".")
                    )
                    base = ".".join([*package, node.module])
                imports.add(base)
        public = [
            (
                f"{node.name}({', '.join(arg.arg for arg in node.args.args)})"
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else node.name
            )
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        ]
        modules[name] = {
            "file": relative,
            "imports": imports,
            "interfaces": public,
            "doc": ast.get_docstring(tree) or "",
        }

    if not modules:
        raise KioError(
            f"There is no Python code in {root} to recover an architecture from."
        )

    components = []
    for name, module in modules.items():
        local = sorted(
            other
            for other in modules
            if other != name
            and any(
                imported == other or imported.startswith(other + ".")
                for imported in module["imports"]
            )
        )
        components.append(
            {
                "name": name,
                "kind": "module",
                "file": module["file"],
                "responsibility": (
                    module["doc"].splitlines()[0] if module["doc"] else ""
                ),
                "interfaces": module["interfaces"],
                "requirements": [],
                "depends_on": local,
            }
        )

    graph = {c["name"]: c["depends_on"] for c in components}
    cycles = _cycles(graph)
    used_by: dict[str, int] = defaultdict(int)
    for targets in graph.values():
        for target in targets:
            used_by[target] += 1
    return {
        "summary": f"{len(components)} modules, {sum(len(t) for t in graph.values())} imports between them"
        + (f", {len(cycles)} import cycle(s)." if cycles else ", no cycles."),
        "verdict": "defect" if cycles else "clean",
        "style": "recovered from the import graph",
        "components": components,
        "cycles": cycles,
        "core": sorted(name for name, count in used_by.items() if count >= 2),
        "diagram": _mermaid(
            [
                {**c, "name": snake(c["name"])}
                | {"depends_on": [snake(t) for t in c["depends_on"]]}
                for c in components
            ]
        ),
        "method": "import graph (no model)",
    }


def _cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(node: str, path: list[str]) -> None:
        for target in graph.get(node, []):
            if target in path:
                cycle = path[path.index(target) :]
                key = tuple(sorted(cycle))
                if key not in seen:
                    seen.add(key)
                    found.append([*cycle, target])
            elif len(path) < 12:
                visit(target, [*path, target])

    for start in graph:
        visit(start, [start])
    return found


def _recovering(request: Request) -> bool:
    if request.find_upstream("requirements"):
        return False
    if request.arguments.get("mode") == "recover":
        return True
    return bool(
        request.workspace.root
        and re.search(
            r"\b(existing|current|recover\w*|reverse|as[- ]is|this (?:code|repo\w*|project)|mevcut|var olan)\b",
            request.task,
            re.IGNORECASE,
        )
    )


# ── capabilities ────────────────────────────────────────────────────────────


def architecture_design(request: Request) -> dict[str, Any]:
    if _recovering(request):
        return recover(request)
    found = _requirements(request)
    designed = design(found)
    gaps = designed["traceability"]["untraced_requirements"]
    return {
        "summary": f"{len(designed['components'])} components for {len(found['requirements'])} requirements "
        f"({designed['style']})"
        + (
            f"; {len(gaps)} requirement(s) not traced."
            if gaps
            else "; every requirement traced to a component."
        ),
        "verdict": (
            "defect"
            if gaps
            else "inconclusive" if designed["open_questions"] else "clean"
        ),
        "hitl_required": bool(designed["open_questions"]),
        **designed,
        # Passed on, so KIO7 and KIO6 downstream see what the design answers to.
        "requirements": found["requirements"],
        "nonfunctional_requirements": found.get("nonfunctional_requirements") or [],
        "acceptance_criteria": found.get("acceptance_criteria") or [],
        "document": markdown(found, designed),
        "method": "rule-based layered design (no model)",
    }


def component_design(request: Request) -> dict[str, Any]:
    """Each interface as a contract, and the entities the components work on."""
    components = request.find_upstream("components")
    found = _requirements(request)
    if not (isinstance(components, list) and components):
        components = design(found)["components"]
    criteria = found.get("acceptance_criteria") or []
    by_requirement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for criterion in criteria:
        by_requirement[str(criterion.get("requirement"))].append(criterion)

    contracts = []
    for component in components:
        if not isinstance(component, dict):
            continue
        for interface in component.get("interfaces") or []:
            name = str(interface).split("(")[0]
            parameters = re.search(r"\((.*)\)", str(interface))
            post = [
                f"{c.get('id')}: given {c.get('given')}, when {c.get('when')}, then {c.get('then')}"
                for requirement in component.get("requirements") or []
                for c in by_requirement.get(requirement, [])
                if component.get("kind") == "service"
            ]
            contracts.append(
                {
                    "component": component.get("name"),
                    "operation": name,
                    "inputs": (
                        [p.strip() for p in parameters.group(1).split(",") if p.strip()]
                        if parameters
                        else []
                    ),
                    "returns": "a result the caller shows, or an error that says what was wrong",
                    "raises": (
                        [
                            "ValidationError when the input breaks a rule",
                            "NotFound when the record does not exist",
                        ]
                        if component.get("kind") == "service"
                        else []
                    ),
                    "postconditions": post,
                    "requirements": component.get("requirements") or [],
                }
            )

    entities: dict[str, set[str]] = {}
    for item in found["requirements"]:
        head = _head(item)
        fields = (
            {
                singular(word)
                for word in words(PREPOSITION.split(" " + _goal(item) + " ", 1)[-1])
                if word not in STOP
                and word not in ARTICLES
                and len(word) > 2
                and word != head
            }
            if PREPOSITION.search(" " + _goal(item) + " ")
            else set()
        )
        entities.setdefault(head, {"id"}).update(fields)
    diagram = ["classDiagram"] + [
        line
        for head, fields in entities.items()
        for line in [f"  class {pascal(head)} {{"]
        + [f"    {field}" for field in sorted(fields)]
        + ["  }"]
    ]
    return {
        "summary": f"{len(contracts)} operation contracts across {len(components)} components, "
        f"{len(entities)} entities.",
        "verdict": "clean",
        "contracts": contracts,
        "entities": [
            {"name": pascal(head), "fields": sorted(fields)}
            for head, fields in entities.items()
        ],
        "diagram": "\n".join(diagram),
        "components": components,
        "requirements": found["requirements"],
        "acceptance_criteria": criteria,
        "method": "contracts from interfaces and acceptance criteria (no model)",
    }


KIO = Kio(
    kio_id="KIO4",
    title="Architecture Design and Component Design",
    description=(
        "Software architecture and component design: turns requirements into components with "
        "interfaces and dependencies, two-way requirement-to-component traceability, decision "
        "rationale, a Mermaid diagram and a code skeleton; writes operation contracts from "
        "acceptance criteria; recovers the architecture of existing code from its import graph."
    ),
    capabilities=[
        Capability(
            "architecture_design",
            "Components, dependencies, traceability, decisions and a diagram from requirements; or the architecture of existing code.",
            architecture_design,
        ),
        Capability(
            "component_design",
            "Operation contracts and entities for each component.",
            component_design,
        ),
    ],
)
