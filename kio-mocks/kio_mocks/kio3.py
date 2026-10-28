"""KIO3 — natural language to formal requirements.

D2.6 §4.1.3 and UC-UC1-01: from a request in plain language, the intents and
functional and non-functional requirements (FR-KIO3-01/03), user stories
(FR-KIO3-02), acceptance criteria (FR-KIO3-05), assumptions, limitations and
constraints (FR-KIO3-06), a wireframe when the request describes a screen
(FR-KIO3-04), a structured issue with priority, tags and story points, and
all of it as Markdown to export (FR-KIO3-07).

Without a model this is pattern work, and it is honest about it: every
requirement quotes the words it came from, a list in the request ("including
a cart, discounts and invoices") becomes one requirement per item, a number
with a threshold becomes boundary criteria on both sides of it, and a vague
word ("fast", "easy") is not guessed at — it becomes an ambiguity for a person
to settle (FR-KIO3-08's review), with `hitl_required` set.
"""

from __future__ import annotations

import re
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.text import (
    ACTORS,
    STOP,
    actor,
    concerns,
    is_action,
    key_terms,
    sentences,
    vague_terms,
)

# What the planner adds about the deliverable, not the product: dropped.
META = re.compile(
    r",?\s*(?:and\s+)?(?:please\s+)?(?:write|draft|create|derive|produce|generate|define|list|give)\s+"
    r"(?:me\s+)?(?:the\s+|its\s+)?(?:(?:formal|natural[- ]language|functional|detailed|clear)\s+)*"
    r"(?:requirements?|user stories|stories|acceptance criteria|an? issue)"
    r"(?:\s+(?:for|of)\b)?|,?\s*(?:gereksinimlerini|gereksinimleri|kullanıcı hikayelerini|kabul kriterlerini)\s+"
    r"(?:yaz|çıkar|oluştur|üret)\w*"
    r"|,?\s*(?:and\s+)?(?:check|verify|validate|compare)\s+(?:the\s+|this\s+|our\s+)?"
    r"(?:code|implementation|repository|repo|project)\b[^.]*"
    # "Include acceptance criteria in the format: '...'" is about the document.
    r"|,?\s*(?:and\s+)?(?:please\s+)?(?:include|add|attach)\s+(?:the\s+|its\s+)?"
    r"(?:acceptance criteria|user stories|an? issue)\b[^.]*\.?['\"]?",
    re.IGNORECASE,
)

LIST_INTRO = re.compile(
    r"\b(?:including|with|such as|supporting|that has|içeren|olan)\b\s*:?\s*(.+)$",
    re.IGNORECASE,
)
# Turkish puts the verb last: "Müşteriler toplu indirim istiyor".
WANT_LAST = re.compile(
    r"^(\S+)\s+(.+?)\s+(?:istiyor\w*|bekliyor\w*|talep ediyor\w*)$", re.IGNORECASE
)
WANT = re.compile(
    r"^(.*?)\b(?:want|wants|need|needs|would like|expect|expects|istiyor|istiyorlar|istiyoruz)\s+(?:to\s+)?(.+)$",
    re.IGNORECASE,
)
MODAL = re.compile(
    r"^(.*?)\b(should|must|shall|can|has to|have to|needs to)\s+(.+)$", re.IGNORECASE
)
PERSON = re.compile(r"\b(users?|kullanıcı\w*)\b", re.IGNORECASE)
THRESHOLD = re.compile(
    r"(\d+)\s*(?:or more|\+|and above|or above|ve üzeri|veya daha fazla|ve fazlası)\s*([a-zçğıöşü]+)?"
    r"[^.%\d]{0,40}?(?:(\d+(?:[.,]\d+)?)\s*%|%\s*(\d+(?:[.,]\d+)?))",
    re.IGNORECASE,
)
# The same rule written rate first: "5% off when buying 5 or more items".
RATE_FIRST = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%[^.%\d]{0,40}?"
    r"(\d+)\s*(?:or more|\+|and above|or above|ve üzeri|veya daha fazla|ve fazlası)\s*([a-zçğıöşü]+)?",
    re.IGNORECASE,
)
MORE = re.compile(
    r"\d+\s*(?:or more|\+|and above|or above|ve üzeri|veya daha fazla|ve fazlası)",
    re.IGNORECASE,
)
# "Customers receive 5% off ...": a person and what happens to them.
DECLARES = re.compile(
    r"^((?:the|a|an|all|every|each)\s+)?(\w+)\s+([a-z]+)\s+(.+)$", re.IGNORECASE
)
OUTCOME = re.compile(
    r"\b(gets?|receives?|earns?|pays?|sees?|has|have)\b", re.IGNORECASE
)
URGENT = re.compile(
    r"\b(urgent|critical|asap|blocker|immediately|acil|kritik|hemen)\b", re.IGNORECASE
)
BUG = re.compile(
    r"\b(bug|error|crash\w*|fail\w*|broken|exception|hata|çök\w*|bozul\w*)\b",
    re.IGNORECASE,
)
UI = re.compile(
    r"\b(page|screen|form|button|dashboard|view|list|table|menu|ekran|sayfa|buton|düğme|tablo|liste)\b",
    re.IGNORECASE,
)
CONSTRAINT = re.compile(
    r"[^.;]*\b(only|must not|without|no more than|at most|at least|within|budget|deadline|yalnızca|sadece|olmadan|en fazla|en az)\b[^.;]*",
    re.IGNORECASE,
)
TECH = re.compile(
    r"\b(python|java|javascript|typescript|react|vue|django|fastapi|flask|postgres\w*|mysql|sqlite|docker|"
    r"kubernetes|aws|azure|rest|graphql|android|ios|c\+\+|rust|go)\b",
    re.IGNORECASE,
)
FIBONACCI = (1, 2, 3, 5, 8, 13)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", META.sub("", text)).strip(" ,.;:")


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _a(noun: str) -> str:
    # "an administrator", "an unknown visitor", but "a user".
    vowel = noun[:1].lower() in "aeio" or noun[:2].lower() == "un"
    return ("an " if vowel else "a ") + noun


def _items(clause: str) -> list[str]:
    """`including a cart, discounts and invoices` → three items."""
    found = LIST_INTRO.search(clause)
    # "a dashboard with filters by date and status" describes one thing;
    # "with" starts a list only when the list has commas.
    if not found or (
        found.group(0).lower().startswith("with") and "," not in found.group(1)
    ):
        return []
    items = re.split(r",\s*|\s+(?:and|or|ve|veya)\s+", found.group(1))
    items = [re.sub(r"^(?:and|or|ve|veya)\s+", "", item.strip(" .")) for item in items]
    return [item for item in items if len(item) > 2]


def _statement(clause: str) -> dict[str, str]:
    """The requirement one clause states, and what kind of statement it is.

    "Customers want to export reports" lets the customer do something (an
    action); "customers want bulk discounts" asks the system to provide
    something (a thing); "invoices must include VAT" is a rule about a thing,
    kept with its own subject. Stories and criteria read differently for each.
    """
    who = actor(clause)
    wanted = WANT.match(clause) or WANT_LAST.match(clause.split(":")[0].strip())
    modal = None if wanted else MODAL.match(clause)
    matched = wanted or modal
    subject = matched.group(1).strip() if matched else ""
    goal = (matched.group(matched.lastindex or 1) if matched else clause).strip()
    goal = re.sub(r"^(?:be able to|to)\s+", "", goal, flags=re.IGNORECASE)
    by_person = bool(wanted) or bool(PERSON.search(subject)) or actor(subject, "") != ""
    found = {"actor": who, "goal": goal, "subject": subject}

    if is_action(goal) and (by_person or not subject):
        return {
            **found,
            "kind": "action",
            "text": f"The system shall let the {who} {goal}",
        }
    if modal and subject and not by_person:
        return {**found, "kind": "rule", "text": f"{_capital(subject)} shall {goal}"}
    if wanted:
        return {**found, "kind": "thing", "text": f"The system shall provide {goal}"}
    declared = None if matched else DECLARES.match(clause)
    if (
        declared
        and actor(declared.group(2), "")
        and declared.group(3).lower() not in STOP
    ):
        subject = (declared.group(1) or "") + declared.group(2)
        verb, rest = declared.group(3), declared.group(4)
        # "Customers buying 5 or more items get 5%": the -ing phrase belongs
        # to the subject, and the verb that follows it is the requirement's.
        consequence = OUTCOME.search(rest) if verb.lower().endswith("ing") else None
        if consequence:
            subject = f"{subject} {verb} {rest[: consequence.start()].strip()}"
            verb, rest = consequence.group(1), rest[consequence.end() :].strip()
        if verb == "has":
            verb = "have"
        elif verb.endswith("s") and not declared.group(2).endswith("s"):
            verb = verb[:-1]
        goal = f"{verb} {rest}"
        return {
            **found,
            "goal": goal,
            "subject": subject,
            "kind": "action",
            "text": f"{_capital(subject)} shall {goal}",
        }
    return {
        **found,
        "kind": "thing",
        "text": f"The system shall support {goal[:1].lower() + goal[1:]}",
    }


def _thresholds(text: str) -> list[tuple[str, str, str]]:
    """(count, unit, rate) for each "N or more ... R%" — whichever way round it is written."""
    after = [(count, unit, a or b) for count, unit, a, b in THRESHOLD.findall(text)]
    first = [(count, unit, rate) for rate, count, unit in RATE_FIRST.findall(text)]
    percent, more = text.find("%"), MORE.search(text)
    rate_first = percent != -1 and more is not None and percent < more.start()
    return (first if rate_first else after) or after or first


def _short(goal: str) -> str:
    """A goal as a story or a criterion quotes it: its first part, a few words."""
    goal = re.split(r":|\s+-\s+|,\s+(?:and|or)\s+", goal.strip().rstrip("."))[0]
    words = goal.split()
    if len(words) <= 16:
        return goal
    return re.sub(r"[\s,]+(?:and|or|ve|veya|with|of|by)$", "", " ".join(words[:12]))


def _story(item: dict[str, Any]) -> str:
    goal = _short(item["goal"])
    wish = {
        "action": f"to {goal}",
        "rule": f"{item['subject'].lower()} to {goal}",
    }.get(item["kind"], goal)
    benefit = _benefit(item["source"]) or "[benefit to confirm]"
    return f"As {_a(item['actor'])}, I want {wish}, so that {benefit}"


def _criterion(item: dict[str, Any]) -> dict[str, str]:
    """The plain Given/When/Then for one requirement; thresholds add their own."""
    goal = _short(item["goal"])
    if item["kind"] == "rule":
        subject = item["subject"].lower()
        then = (
            f"{subject} {goal}"
            if not goal.lower().startswith("be ")
            else f"{subject} is {goal[3:]}"
        )
        return {
            "given": "the system in use",
            "when": f"{subject} are produced",
            "then": then,
        }
    return {
        "given": f"{_a(item['actor'])} using the system",
        "when": f"they {goal}" if item["kind"] == "action" else f"they use {goal}",
        "then": "it works as stated and the result is shown",
    }


def _title(clause: str) -> str:
    """The first clause without its list and its leading article: a name, not a sentence."""
    title = LIST_INTRO.split(clause)[0] if LIST_INTRO.search(clause) else clause
    title = re.split(r":|\s+-\s+", title)[0].strip(" ,")
    title = _capital(
        re.sub(r"^(?:an?|the)\s+", "", title, flags=re.IGNORECASE) or clause
    )
    return (title[:80] + "…") if len(title) > 80 else title


def analyse(text: str) -> dict[str, Any]:
    """Everything KIO3 derives from a request; each capability returns part of it."""
    request = _clean(text)
    clauses = sentences(request) or ([request] if request else [])
    if not clauses:
        raise KioError(
            "There is no request to derive requirements from: the task is empty."
        )

    functional: list[dict[str, Any]] = []
    nonfunctional: list[dict[str, Any]] = []

    for clause in clauses:
        items = _items(clause)
        head = LIST_INTRO.split(clause)[0].strip(" ,") if items else clause
        # A list's lead-in ("a project management SaaS including ...") names
        # the product; it is a requirement only when it asks for something.
        asks = bool(WANT.match(head) or MODAL.match(head))
        if asks and is_action(_statement(head)["goal"]):
            # "Admins want to see orders, refunds and payouts" is one action on a list.
            items = []
            head = clause
        before, colon, after = head.partition(": ")
        if (
            colon
            and not items
            and len(before.split()) <= 6
            and not (
                WANT.match(before)
                or WANT_LAST.match(before)
                or MODAL.match(before)
                or re.search(r"\d", before)
            )
            and len(after.split()) >= 3
        ):
            head = after
        for part in ([head] if not items or asks else []) + items:
            kinds = concerns(part)
            if kinds and not items:
                nonfunctional.append({"category": kinds[0], "text": part})
                continue
            functional.append({**_statement(part), "source": clause})
        # A list's own items are features ("user authentication"); only a
        # concern the rest of the clause raises is a quality to add.
        named = {kind for item in items for kind in concerns(item)}
        for kind in concerns(head) if items else []:
            if kind not in named:
                nonfunctional.append({"category": kind, "text": clause})

    for index, item in enumerate(functional, 1):
        item["id"] = f"FR-{index:02d}"
    for index, item in enumerate(nonfunctional, 1):
        item["id"] = f"NFR-{index:02d}"

    stories = [
        {"id": f"US-{index:02d}", "requirement": item["id"], "text": _story(item)}
        for index, item in enumerate(functional, 1)
    ]

    criteria = []
    for item in functional:
        criteria.append({"requirement": item["id"], **_criterion(item)})
        for threshold, unit, rate in _thresholds(item["source"]):
            noun = unit or "items"
            criteria.append(
                {
                    "requirement": item["id"],
                    "given": f"{threshold} {noun}",
                    "when": "the total is calculated",
                    "then": f"a {rate}% discount applies",
                }
            )
            criteria.append(
                {
                    "requirement": item["id"],
                    "given": f"{int(threshold) - 1} {noun}",
                    "when": "the total is calculated",
                    "then": f"the {rate}% discount does not apply",
                }
            )
    # Keep the threshold criteria once even when several clauses quote them.
    seen, unique = set(), []
    for criterion in criteria:
        key = (criterion["given"], criterion["when"], criterion["then"])
        if key not in seen:
            seen.add(key)
            unique.append(criterion)
    criteria = [
        {"id": f"AC-{index:02d}", **criterion}
        for index, criterion in enumerate(unique, 1)
    ]

    vague = vague_terms(request)
    ambiguities = [
        f"“{term}” is not measurable as written; give a number or a test for it."
        for term in vague
    ]
    assumptions = []
    if all(item["actor"] == "user" for item in functional):
        assumptions.append(
            "The actor is a signed-in end user; no other role was named."
        )
    for item in nonfunctional:
        if item["category"] == "performance" and not re.search(r"\d", item["text"]):
            assumptions.append(
                "Performance is taken as a 300 ms response at the 95th percentile until stated."
            )
    constraints = sorted(
        {match.group(0).strip() for match in CONSTRAINT.finditer(request)}
    ) + [
        f"Technology named: {name}"
        for name in sorted({m.lower() for m in TECH.findall(request)})
    ]

    # Tags from the request's own words, not from the "shall" sentences made of them.
    terms = [
        term
        for term in key_terms(clauses, limit=10)
        if term not in vague and not is_action(term)
    ] or key_terms([request])
    size = len(functional) + len(nonfunctional)
    issue = {
        "title": _title(clauses[0]),
        "type": "bug" if BUG.search(request) else "feature",
        "priority": "High" if URGENT.search(request) else "Medium",
        "tags": terms[:4],
        "story_points": FIBONACCI[min(len(FIBONACCI) - 1, max(0, size - 1))],
    }

    return {
        "requirements": functional,
        "nonfunctional_requirements": nonfunctional,
        "user_stories": stories,
        "acceptance_criteria": criteria,
        "assumptions": assumptions,
        "constraints": constraints,
        "ambiguities": ambiguities,
        "issue": issue,
        "wireframe": (_wireframe(request) if UI.search(request) else None),
    }


def _benefit(clause: str) -> str:
    found = re.search(
        r"\b(?:so that|in order to|because|so they can|böylece|ki)\s+(.+)$",
        clause,
        re.IGNORECASE,
    )
    return found.group(1).strip(" .") if found else ""


def _wireframe(request: str) -> str:
    """A box for the screen the request names, with the fields the clause that names it mentions."""
    screen = next(
        (clause for clause in sentences(request) if UI.search(clause)), request
    )
    title = _title(screen)
    verb = (_statement(screen)["goal"].split() or [""])[0].lower()
    fields = [
        term
        for term in key_terms([screen], limit=10)
        if not UI.fullmatch(term) and term not in ACTORS and term != verb
    ]
    width = 46
    lines = [
        "+" + "-" * width + "+",
        "| " + title[: width - 2].ljust(width - 2) + " |",
        "+" + "-" * width + "+",
    ]
    for field in fields[:4]:
        lines.append(
            "| "
            + f"{field.capitalize():<12} [______________________]".ljust(width - 2)
            + " |"
        )
    lines += [
        "| " + "[ Cancel ]   [ Save ]".rjust(width - 2) + " |",
        "+" + "-" * width + "+",
    ]
    return "\n".join(lines)


def markdown(found: dict[str, Any]) -> str:
    """The whole specification as Markdown, to export (FR-KIO3-07)."""
    issue = found["issue"]
    out = [
        f"# {issue['title']}",
        "",
        (
            f"**{issue['type'].capitalize()}** · priority {issue['priority']} · "
            f"{issue['story_points']} story points · tags: {', '.join(issue['tags'])}"
        ),
        "",
        "## Functional requirements",
        "",
    ]
    out += [f"- **{item['id']}** {item['text']}." for item in found["requirements"]]
    if found["nonfunctional_requirements"]:
        out += ["", "## Non-functional requirements", ""]
        out += [
            f"- **{item['id']}** ({item['category']}) {item['text']}."
            for item in found["nonfunctional_requirements"]
        ]
    out += ["", "## User stories", ""] + [
        f"- **{story['id']}** {story['text']}." for story in found["user_stories"]
    ]
    out += ["", "## Acceptance criteria", ""] + [
        f"- **{c['id']}** ({c['requirement']}) Given {c['given']}, when {c['when']}, then {c['then']}."
        for c in found["acceptance_criteria"]
    ]
    for title, key in (
        ("Assumptions", "assumptions"),
        ("Constraints", "constraints"),
        ("To confirm", "ambiguities"),
    ):
        if found[key]:
            out += ["", f"## {title}", ""] + [f"- {item}" for item in found[key]]
    if found["wireframe"]:
        out += ["", "## Wireframe", "", "```", found["wireframe"], "```"]
    return "\n".join(out) + "\n"


def _request_text(request: Request) -> str:
    return str(request.arguments.get("text") or request.task)


def _verdict(found: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "inconclusive" if found["ambiguities"] else "clean",
        "hitl_required": bool(found["ambiguities"]),
    }


def requirements_engineering(request: Request) -> dict[str, Any]:
    found = analyse(_request_text(request))
    return {
        "summary": (
            f"{len(found['requirements'])} functional and {len(found['nonfunctional_requirements'])} non-functional "
            f"requirements, {len(found['acceptance_criteria'])} acceptance criteria"
            + (
                f"; {len(found['ambiguities'])} to confirm."
                if found["ambiguities"]
                else "."
            )
        ),
        **_verdict(found),
        **found,
        "document": markdown(found),
        "method": "rule-based (no model)",
    }


def user_stories(request: Request) -> dict[str, Any]:
    found = analyse(_request_text(request))
    return {
        "summary": f"{len(found['user_stories'])} user stories, issue “{found['issue']['title']}” "
        f"({found['issue']['story_points']} points, {found['issue']['priority']}).",
        **_verdict(found),
        "user_stories": found["user_stories"],
        "requirements": found["requirements"],
        "issue": found["issue"],
        "ambiguities": found["ambiguities"],
    }


def acceptance_criteria(request: Request) -> dict[str, Any]:
    upstream = request.find_upstream("requirements")
    text = (
        ". ".join(
            item.get("source") or item.get("text", "")
            for item in upstream
            if isinstance(item, dict)
        )
        if isinstance(upstream, list) and upstream
        else _request_text(request)
    )
    found = analyse(text)
    return {
        "summary": f"{len(found['acceptance_criteria'])} acceptance criteria for {len(found['requirements'])} requirements.",
        **_verdict(found),
        "acceptance_criteria": found["acceptance_criteria"],
        "requirements": found["requirements"],
    }


KIO = Kio(
    kio_id="KIO3",
    title="Natural Language Processing to Formal Requirements",
    description=(
        "Natural-language requirements engineering, user stories, and acceptance criteria: "
        "turns a request into functional and non-functional requirements, user stories, "
        "Given/When/Then criteria with boundary cases, assumptions, constraints, a structured "
        "issue and a Markdown document, and flags what is too vague to test."
    ),
    capabilities=[
        Capability(
            "requirements_engineering",
            "Formal requirements, criteria, issue and document from a request.",
            requirements_engineering,
        ),
        Capability(
            "user_stories",
            "User stories and a structured issue from a request.",
            user_stories,
        ),
        Capability(
            "acceptance_criteria",
            "Given/When/Then criteria, boundaries included.",
            acceptance_criteria,
        ),
    ],
)
