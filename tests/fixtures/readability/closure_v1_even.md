# Proof

This proof is the verified fact closure of the target theorem. Each lemma and the main theorem were accepted one at a time by an independent, response-only model verifier that saw only that statement, its proof, and the statements of the facts it cites. Model-verifier acceptance is not a Lean or other formal certificate.

## Lemma 1 · fact 57e467472fd97ed4

**Uses:** none.

**Statement.**

For all natural numbers n and k, if n = 2k + 1 then there exists a natural number m with n^2 + n = 2m.

**Proof.**

Fix natural k and set n = 2k + 1. Then
n^2 + n = n(n + 1) = (2k + 1)(2k + 2) = 2((2k + 1)(k + 1)).
Set m = (2k + 1)(k + 1), a natural number since both factors are natural. Then n^2 + n = 2m, m witnesses evenness.

## Lemma 2 · fact a9621d30a1c72370

**Uses:** none.

**Statement.**

For every natural number n (a nonnegative integer), there exists a natural number k such that n = 2k or n = 2k + 1.

**Proof.**

By induction on n.
Base case n = 0: take k = 0; then 2k = 0 = n, so the claim holds with the first alternative.
Inductive step: assume for a fixed natural n there exists a natural k with n = 2k or n = 2k + 1. Consider n + 1.
Case 1: n = 2k. Then n + 1 = 2k + 1, so the second alternative holds for n + 1 with the same k.
Case 2: n = 2k + 1. Then n + 1 = 2k + 2 = 2(k + 1), and k + 1 is a natural number, so the first alternative holds for n + 1 with k' = k + 1.
In both cases the claim holds for n + 1. By induction it holds for every natural n.

## Lemma 3 · fact cb723533e35aff2e

**Uses:** none.

**Statement.**

For all natural numbers n and k, if n = 2k then there exists a natural number m with n^2 + n = 2m.

**Proof.**

Fix natural k and set n = 2k. Then
n^2 + n = (2k)^2 + 2k = 4k^2 + 2k = 2(2k^2 + k).
Set m = 2k^2 + k, which is a natural number since k is. Then n^2 + n = 2m, m witnesses evenness.

## Main theorem · fact 1914e15b87add6fe

**Uses:** Lemma 1 (fact 57e467472fd97ed4), Lemma 2 (fact a9621d30a1c72370), Lemma 3 (fact cb723533e35aff2e).

**Statement.**

Prove that for every natural number n, n^2 + n is even.

**Proof.**

Cited facts: L1 = fact a9621d30a1c72370; L2 = fact cb723533e35aff2e; L3 = fact 57e467472fd97ed4.

Define: a natural number x is even if there exists a natural number m with x = 2m.
Fix any natural number n. By L1, there exists a natural k with n = 2k or n = 2k + 1.
Case 1: n = 2k. By L2, there exists a natural m with n^2 + n = 2m, so n^2 + n is even.
Case 2: n = 2k + 1. By L3, there exists a natural m with n^2 + n = 2m, so n^2 + n is even.
In either case n^2 + n is even. Since n was arbitrary, for every natural number n, n^2 + n is even.
