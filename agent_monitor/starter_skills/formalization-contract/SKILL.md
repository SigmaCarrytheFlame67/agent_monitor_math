---
name: formalization-contract
description: Preserve mathematical meaning between an original problem, informal proof, and Lean formalization. Use when translating to Lean, reviewing formal coverage, auditing quantifiers or assumptions, or deciding whether formal verification supports the original claim.
---

# Formalization contract

1. Assign stable identifiers to the original claim and every lemma used by the informal proof.
2. Map each identifier to its exact quantifiers, domains, hypotheses, constants, conclusion, and Lean declaration.
3. Run a statement-fidelity preflight when available, then perform a semantic mathematical review; a clear preflight is not proof of equivalence.
4. Reject weakened conclusions, strengthened or inconsistent assumptions, changed endpoint conditions, and vacuous encodings.
5. Compile every declaration and report sorry or admit per declaration.
6. Mark a statement formally covered only when a faithful, admission-free declaration proves it.
7. Keep unmatched informal statements gray and preserve verified helper declarations even when the main theorem remains incomplete.
8. Rebuild formal DAG and coverage after any informal or Lean edit.
9. State first: compile and audit the main statement (all proofs `sorry`) before spending proof effort, then keep that statement locked while proving.
10. Tag each Lean lemma that formalizes an informal fact or DAG node with a doc line `Informal: <id>`; coverage maps by these ids before any lexical matching.
11. Target the pinned toolchain exactly (Lean 4.14.0, Mathlib v4.14.0, December 2024). `import Mathlib` is supported; newer lemma names may not exist.
12. Treat a clean compile as verified only when `#print axioms` for every declaration (the main theorem by its exact, root-qualified name) shows nothing beyond `propext`, `Classical.choice` and `Quot.sound`, and the proved main statement is the one that was audited (unchanged text lock and Lean statement fingerprint); otherwise audit the final file again.

Kernel acceptance certifies only the encoded theorem. It does not by itself certify fidelity to the original problem.
