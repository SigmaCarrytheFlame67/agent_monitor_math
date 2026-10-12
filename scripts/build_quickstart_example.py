"""Build the Quickstart's worked example from one finished Ansatz CGM run.

The public docs show a real guest run ("Prove that the sum of two odd integers
is even."). This script copies only what the Quickstart needs from that run's
workspace and record, drops everything tied to a person or a server (owner and
guest ids, run and job ids, cookies, absolute paths, logs), and writes:

  docs/.vitepress/theme/quickstartExample.json   data for the Quickstart page
                                                 and the read-only run viewer
  docs/public/examples/odd-sum/Proof.lean        the exact file Lean accepted

It makes no model calls and never modifies the run. Usage:

  python3 scripts/build_quickstart_example.py \
      --workspace /var/lib/ansatze/console/runs/workspaces/<run_id> \
      --record /var/lib/ansatze/console/runs/<run_id>.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "docs/.vitepress/theme/quickstartExample.json"
OUT_LEAN = ROOT / "docs/public/examples/odd-sum/Proof.lean"
PUBLIC_LEAN = "/examples/odd-sum/Proof.lean"
PROBLEM = "Prove that the sum of two odd integers is even."

# Short card titles for the Facts graph. They summarise the statements shown in
# full beside them; the proof text itself is never rewritten.
SHORT = {
    "Lemma 1": r"Odd: $x = 2k + 1$; even: $x = 2t$",
    "Lemma 2": r"$\mathbb{Z}$ is closed under addition",
    "Lemma 3": r"If $a$ and $b$ are odd, $a + b$ is even",
    "Target theorem": "The sum of two odd integers is even",
}

# The curator's deterministic reasons for a plan's status (agent_monitor/cgm/curation.py).
PLAN_RULES = {
    "Deterministic: this plan's attempt produced the accepted target.": "accepted-target",
    "Deterministic: a member of its AND group is dead.": "dead-member",
}

# Anything matching these must never reach the public site.
FORBIDDEN = [
    r"/var/", r"/opt/", r"/home/", r"/tmp/", r"/etc/", r"@public\.local", r"guest\+",
    r"owner", r"cookie", r"\bsk-", r"adhoc_", r"kimi_adhoc", r"sponsored",
]


def tex(text: str) -> str:
    """Typeset the inline mathematics of a CGM proof without changing its words.

    Maximal runs of single-letter variables, numerals, Z/R, parentheses, commas
    and the operators = + / ∈ ≡ that contain an operator become $…$ spans; a
    comma after Z or R ends a span. Outside those spans a lone Z or R (the
    integers, the reals) becomes blackboard bold and a lone variable is set in
    math italic. The article "a" stays prose unless it reads as a variable.
    """
    lexeme = re.compile(
        r"(?P<set>Z/2Z|(?<![A-Za-z0-9])[ZR](?![A-Za-z0-9]))"
        r"|(?P<term>\(mod \d+\)|(?<![A-Za-z0-9])\d+[a-z]?(?![A-Za-z0-9])|(?<![A-Za-z0-9])[a-z](?![A-Za-z0-9]))"
        r"|(?P<op>[=+/∈≡])|(?P<paren>[()])|(?P<comma>,)|(?P<space>[ \t]+)"
        r"|(?P<other>.)",
        re.S,
    )
    symbols = {"Z": r"\mathbb{Z}", "R": r"\mathbb{R}", "Z/2Z": r"\mathbb{Z}/2\mathbb{Z}", "∈": r"\in", "≡": r"\equiv"}
    matches = list(lexeme.finditer(text))
    parts: list[tuple[str, str]] = [(m.lastgroup, m.group()) for m in matches]

    def symbol(value: str) -> str:
        match = re.fullmatch(r"\(mod (\d+)\)", value)
        return rf" \pmod{{{match.group(1)}}}" if match else symbols.get(value, value)

    def context(index: int) -> tuple[str, str]:
        return text[:matches[index].start()], text[matches[index].end():]

    def variable_article(index: int) -> bool:
        """Whether a lone "a" at parts[index] is the variable a rather than the article."""
        before, after = context(index)
        return bool(re.match(r"(?: (?:is|and|be|were|are)\b|,)", after) or re.search(r"\b(?:of|and|both) $", before))

    def label_or_abbreviation(index: int) -> bool:
        """(a) and (b) enumerate; i.e. and e.g. abbreviate; 's is a suffix."""
        before, after = context(index)
        return bool(
            (before.endswith("(") and after.startswith(")"))
            or re.match(r"\.[a-z]\.", after) or re.search(r"[a-z]\.$", before)
            or before.endswith(("'", "\u2019"))
        )

    def prose(kind: str, value: str, index: int) -> str:
        if kind == "set":
            return "$" + symbol(value) + "$"
        if kind != "term" or not re.fullmatch(r"[a-z]", value) or label_or_abbreviation(index):
            return value
        if value != "a" or variable_article(index):
            return "$" + value + "$"
        return value

    out: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i][0] == "other":
            out.append(parts[i][1])
            i += 1
            continue
        j = i
        while j < len(parts) and parts[j][0] != "other" and not (
            parts[j][0] == "comma" and j > i and parts[j - 1][0] == "set"
        ):
            j += 1
        run = list(enumerate(parts[i:j], start=i))
        lead: list = []
        tail: list = []
        while run and run[0][1][0] in {"space", "comma"}:
            lead.append(run.pop(0))
        while run and run[-1][1][0] in {"space", "comma"}:
            tail.insert(0, run.pop())
        # Brackets that do not pair up inside the span stay in the prose.
        count = lambda items, ch: sum(v == ch for _, (_, v) in items)
        while run and (run[-1][1][1] == "(" or (run[-1][1][1] == ")" and count(run, ")") > count(run, "("))):
            tail.insert(0, run.pop())
        while run and (run[0][1][1] == ")" or (run[0][1][1] == "(" and count(run, "(") > count(run, ")"))):
            lead.append(run.pop(0))
        is_math = any(k == "op" for _, (k, _) in run) and any(k in {"term", "set"} for _, (k, _) in run)
        out.extend(v for _, (_, v) in lead)
        if is_math:
            out.append("$" + "".join(symbol(v) for _, (_, v) in run).strip() + "$")
        else:
            out.extend(prose(k, v, index) for index, (k, v) in run)
        out.extend(v for _, (_, v) in tail)
        if j < len(parts) and parts[j][0] == "comma":
            out.append(",")
            j += 1
        i = j
    return "".join(out)


def parse_proof(markdown: str) -> tuple[str, list[dict]]:
    """Split proof.md into its preamble and its numbered facts."""
    sections = re.split(r"^## ", markdown, flags=re.M)
    intro = re.sub(r"^# Proof\s*", "", sections[0]).strip()
    items = []
    for section in sections[1:]:
        head, _, body = section.partition("\n")
        match = re.match(r"(Lemma \d+|Main theorem) · fact ([0-9a-f]{16})$", head.strip())
        assert match, head
        uses = re.search(r"\*\*Uses:\*\* (.+)", body).group(1).strip()
        statement = re.search(r"\*\*Statement\.\*\*\s*(.+?)\s*\*\*Proof\.\*\*", body, re.S).group(1).strip()
        proof = body.split("**Proof.**", 1)[1].strip()
        items.append({
            "label": match.group(1),
            "fact": match.group(2),
            "uses": [] if uses == "none." else re.findall(r"(Lemma \d+)", uses),
            "statement": statement,
            "proof": proof,
        })
    return intro, items


def first_sentences(text: str, count: int = 2) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:count])


def seconds_between(start: str, end: str) -> int:
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    return round((parse(end) - parse(start)).total_seconds())


def build(workspace: Path, record_path: Path) -> dict:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record.get("status") == "finished", "the run must be finished"
    assert (workspace / "problem.txt").read_text(encoding="utf-8").strip() == PROBLEM

    proof_md = (workspace / "proof.md").read_text(encoding="utf-8")
    intro, items = parse_proof(proof_md)
    facts = json.loads((workspace / "proof_graph.json").read_text(encoding="utf-8"))
    explore = json.loads((workspace / "engine_workflow_graph.json").read_text(encoding="utf-8"))
    verify = json.loads((workspace / "lean_verify.json").read_text(encoding="utf-8"))
    formal = json.loads((workspace / "lean_proof_graph.json").read_text(encoding="utf-8"))
    coverage = json.loads((workspace / "proof_coordination.json").read_text(encoding="utf-8"))
    pipeline = json.loads((workspace / "auto_pipeline.json").read_text(encoding="utf-8"))

    # The Lean file must be byte-for-byte the one the kernel accepted.
    lean_bytes = (workspace / "lean/Proof.lean").read_bytes()
    lean_sha = hashlib.sha256(lean_bytes).hexdigest()
    receipt = verify["kernel_receipt"]
    assert verify["status"] == "verified" and receipt["ok"] and receipt["status"] == "verified"
    assert lean_sha == verify["verified_source_sha256"] == receipt["source_sha256"], "Lean source changed after verification"
    assert verify["lean"].encode("utf-8") == lean_bytes
    assert verify["sorry_count"] == 0
    prove_check = next(a["check"] for a in verify["attempts"] if a["action"] == "prove")
    axioms = prove_check.get("axioms") or {}
    assert axioms.get("ok") and not axioms.get("unexpected")
    audit = verify.get("statement_audit") or {}
    assert audit.get("faithful") is True

    links = {item["informal_id"]: item for item in coverage["items"]}
    graph_nodes = {node["id"]: node for node in facts["graph"]["nodes"]}
    verdicts = {}
    for line in (workspace / "math_harness/project/global_memory/verification.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        report = json.loads(entry["evidence"])
        verdicts[entry["fact_id"]] = {
            "verdict": report.get("verdict"),
            "summary": tex(first_sentences(report.get("verification_report", {}).get("summary", ""))),
        }

    lemmas = []
    for item in items:
        node = graph_nodes[item["fact"]]
        link = links[item["fact"]]
        label = node["label"]
        assert link["status"] == "verified"
        lemmas.append({
            "id": item["fact"],
            "label": label,
            "short": SHORT[label],
            "uses": item["uses"],
            "statement": item["statement"],
            "statement_tex": tex(item["statement"]),
            "proof": item["proof"],
            "proof_tex": tex(item["proof"]),
            "verifier": verdicts[item["fact"]],
            "lean_decl": link["lean_declarations"][0],
        })
    assert [lemma["verifier"]["verdict"] for lemma in lemmas] == ["correct"] * 4

    # A compact view of the exploration graph: plans, their AND subgoals, the
    # worker attempt for each plan, and the verifier's verdicts.
    xnodes = explore["graph"]["nodes"]
    xedges = explore["graph"]["edges"]
    kind = lambda node: node.get("meta", {}).get("workflow_kind") or ("fact" if node["kind"] in {"lemma", "conclusion"} else node["kind"])
    counts: dict[str, int] = {}
    for node in xnodes:
        counts[kind(node)] = counts.get(kind(node), 0) + 1
    by_id = {node["id"]: node for node in xnodes}
    plans = []
    plain_approach = {}  # the run viewer shows text without typeset math
    for plan in (n for n in xnodes if kind(n) == "plan"):
        meta = plan["meta"]
        subgoals = [by_id[e["to"]] for e in xedges if e["from"] == plan["id"] and e["label"] == "decomposes-into"]
        attempt = next(by_id[e["to"]] for e in xedges if e["from"] == plan["id"] and e["label"] == "refines")
        attempt_verdicts = [by_id[e["to"]] for e in xedges if e["from"] == attempt["id"] and e["label"] == "verifier-input"]
        promoted = [e["to"] for e in xedges if e["from"] == attempt["id"] and e["label"] == "promoted-to"]
        title = plan["label"].split(":", 1)[1].strip()
        technique = re.search(r"Technique:\s*(\S+)", plan["statement"]).group(1)
        approach = re.search(r"Approach:\s*(.+?)(?:\n|$)", plan["statement"]).group(1).strip()
        plain_approach[plan["label"].split(":", 1)[0]] = first_sentences(approach, 1)
        comment = meta.get("comment") or ""
        rule = next((name for prefix, name in PLAN_RULES.items() if comment.startswith(prefix)), None)
        if rule == "accepted-target":
            # The Quickstart says the open subgoals of a plan closed this way are already inside the
            # accepted proof; fail the build if the curator's notes ever stop saying so.
            for goal in subgoals:
                if goal["meta"].get("status") == "open":
                    assert re.search(r"(?:implicit|contained) in the accepted theorem", goal["meta"].get("comment", "")), goal["id"]
        plans.append({
            "label": plan["label"].split(":", 1)[0],
            "title": title,
            "technique": technique,
            "approach": tex(first_sentences(approach, 1)),
            "status": meta.get("status"),
            "score": meta.get("score"),
            "rule": rule,
            "subgoals": [{
                "label": goal["label"],
                "statement": tex(re.sub(r"^S\d+:\s*", "", goal["statement"])),
                "status": goal["meta"].get("status"),
                "score": float(goal["meta"]["score"]) if goal["meta"].get("score") not in (None, "") else None,
            } for goal in subgoals],
            "attempt": {
                "label": re.sub(r"\s*\[.*\]$", "", attempt["label"]).split(":", 1)[0],
                "result": re.search(r"\[(.*)\]$", attempt["label"]).group(1),
                "status": attempt["meta"].get("status"),
            },
            "verdicts": [list(re.search(r"on (\S+) in attempt \d+: (\w+)", v["label"]).groups()) for v in attempt_verdicts],
            "facts": len(promoted),
        })
    obstacles = [n["label"] for n in xnodes if kind(n) == "obstacle"]

    stages = pipeline["stages"]
    lean_seconds = seconds_between(stages["lean"]["started_at"], stages["lean"]["finished_at"])
    totals = record["totals"]
    captured = record["created_at"][:10]
    toolchain = re.search(r"version ([\d.]+)", receipt["toolchain"]).group(1)
    checks = [{
        "action": attempt["action"],
        "status": attempt["check"]["status"],
        "sorry": attempt["sorry_count"],
        "seconds": round(attempt["check"]["duration_s"], 1),
    } for attempt in verify["attempts"]]
    main_signature = verify["statement_lock"]["signature"]

    example = {
        "version": 1,
        "type": "example",
        "slug": "odd-sum",
        "title": "The sum of two odd integers is even",
        "captured": captured,
        "engine": "Ansatz CGM · Kimi K3",
        "session": "Guest session on ansatze.ai",
        "status": "Proof accepted by the model verifier · Lean 4 file kernel-checked",
        "problem": PROBLEM,
        "notice": (
            f"A real guest run from {captured}, copied unedited from its workspace. Lemmas were accepted by an "
            "independent model verifier, which is not a formal certificate; the Lean file was checked by Lean's kernel."
        ),
        "stats": {
            "informal_seconds": round(totals["latency_s"]),
            "lean_seconds": lean_seconds,
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "facts": len(lemmas),
            "explore_nodes": len(xnodes),
            "explore_edges": len(xedges),
        },
        "proof_intro": intro,
        "lemmas": lemmas,
        "facts_graph": {
            "basis": facts["basis"],
            "nodes": [{"id": n["id"], "label": n["label"], "kind": n["kind"]} for n in facts["graph"]["nodes"]],
            "edges": [{"from": e["from"], "to": e["to"]} for e in facts["graph"]["edges"]],
        },
        "explore": {
            "counts": counts,
            "nodes": len(xnodes),
            "edges": len(xedges),
            "plans": plans,
            "obstacles": obstacles,
        },
        "lean": {
            "status": receipt["status"],
            "file": PUBLIC_LEAN,
            "lines": lean_bytes.decode("utf-8").count("\n"),
            "source_sha256": lean_sha,
            "toolchain": f"Lean {toolchain} + Mathlib",
            "checker": "Isolated Lean checker (sandboxed broker)",
            "sorry": verify["sorry_count"],
            "axioms": axioms["axioms"],
            "main_decl": verify["main_decl"],
            "signature": main_signature,
            "audit": {"faithful": audit["faithful"], "verdict": audit["verdict"], "back_translation": audit["back_translation"]},
            "checks": checks,
            "coverage": {**coverage["summary"], "links": [
                {"informal": lemma["label"], "lean": lemma["lean_decl"]} for lemma in lemmas
            ]},
        },
        # Keys read by the standalone run viewer (agent_monitor/web/run-viewer.js).
        "proof": proof_md,
        "lean_source": lean_bytes.decode("utf-8"),
        "verification": {
            "version": f"Lean {toolchain} + Mathlib, sandboxed kernel check",
            "result": f"verified · no sorry · axioms used by {verify['main_decl']}: {', '.join(axioms['axioms'])} · SHA-256 {lean_sha[:12]}…",
        },
        "agents": [
            {"label": "Strategist", "status": f"{len(plans)} plans · {len(obstacles)} obstacles noted", "summary": "; ".join(f"{p['label']}: {p['title']} ({p['technique']})" for p in plans) + "."},
            *({"label": f"Worker · {p['label']}", "status": p["attempt"]["result"], "summary": f"{p['title']}: {plain_approach[p['label']]}"} for p in plans),
            {"label": "Independent verifier", "status": "4 of 4 accepted (model verifier, not a Lean certificate)", "summary": " ".join(f"{l['label']}: {l['verifier']['verdict']}." for l in lemmas)},
            {"label": "Curator", "status": ", ".join(f"{p['label']} {p['status']}" for p in plans), "summary": "Scored the exploration graph; the proof is the verified fact closure of the target theorem."},
            {"label": "Lean 4 stage", "status": "verified by the kernel", "summary": f"{main_signature} · {audit['verdict']}"},
        ],
        "informal_graph": {
            "title": "Facts · model-verifier accepted (not a Lean certificate)",
            "nodes": [{"id": l["id"], "label": l["label"], "kind": graph_nodes[l["id"]]["kind"], "statement": l["statement"]} for l in lemmas],
            "edges": [{"from": e["from"], "to": e["to"], "label": "uses"} for e in facts["graph"]["edges"]],
        },
        "formal_graph": {
            "title": "Lean declarations · kernel-checked",
            "nodes": [{"id": n["id"], "label": n["label"], "kind": n["kind"], "lean_kind": n.get("lean_kind"), "statement": n["statement"]} for n in formal["graph"]["nodes"]],
            "edges": [{"from": e["from"], "to": e["to"], "label": e.get("label", "uses")} for e in formal["graph"]["edges"]],
        },
    }

    serialized = json.dumps(example, ensure_ascii=False)
    for pattern in FORBIDDEN + [re.escape(record["run_id"]), re.escape(str(record.get("job_id", "-")))]:
        assert not re.search(pattern, serialized, re.I), f"private data matched {pattern!r}"
    for pattern in FORBIDDEN:
        assert not re.search(pattern, lean_bytes.decode("utf-8"), re.I), f"private data in Proof.lean: {pattern!r}"
    return example, lean_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    example, lean_bytes = build(args.workspace, args.record)
    OUT_JSON.write_text(json.dumps(example, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    OUT_LEAN.parent.mkdir(parents=True, exist_ok=True)
    OUT_LEAN.write_bytes(lean_bytes)
    print(f"Wrote {OUT_JSON.relative_to(ROOT)} and {OUT_LEAN.relative_to(ROOT)} "
          f"({example['stats']['facts']} facts, Explore {example['stats']['explore_nodes']} nodes, Lean {example['lean']['status']}).")


if __name__ == "__main__":
    main()
