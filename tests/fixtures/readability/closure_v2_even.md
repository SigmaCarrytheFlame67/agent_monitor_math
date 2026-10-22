# Proof

This proof is the verified fact closure of the target theorem. Each lemma and the main theorem were accepted one at a time by an independent, response-only model verifier that saw only that statement, its proof, and the statements of the facts it cites. Model-verifier acceptance is not a Lean or other formal certificate.

## Lemma L1 · fact 95f39a5498b2ebea

**Uses:** none.

**Statement.**

For every natural number n (where natural numbers are 0, 1, 2, ...), there exists a natural number k such that n = 2k or n = 2k + 1.

**Proof.**

We prove this by induction on n, using the standard induction principle: if a property P(n) of natural numbers holds for n = 0, and for every natural number n the implication P(n) ⇒ P(n+1) holds, then P(n) holds for all natural numbers n. Let P(n) be: "there exists a natural number k with n = 2k or n = 2k + 1".
Base case n = 0: take k = 0. Then 2·0 = 0 = n, so the first alternative holds.
Induction step: let n be a natural number and assume P(n), i.e., there is a natural number k with n = 2k or n = 2k + 1. Case 1: n = 2k. Then n + 1 = 2k + 1, so the second alternative holds for n + 1 with the same k. Case 2: n = 2k + 1. Then n + 1 = 2k + 2 = 2(k + 1), and k + 1 is a natural number, so the first alternative holds for n + 1 with k' = k + 1. In both cases P(n+1) holds.
By induction, P(n) holds for every natural number n.

## Lemma L2 · fact 941ea68438409973

**Uses:** none.

**Statement.**

Let k be a natural number (where natural numbers are 0, 1, 2, ...). Set n = 2k. Then n^2 + n = 2(2k^2 + k), and 2k^2 + k is a natural number.

**Proof.**

Compute using distributivity and commutativity of natural number arithmetic:
n^2 + n = (2k)^2 + 2k = 4k^2 + 2k = 2(2k^2) + 2(k) = 2(2k^2 + k).
Since natural numbers are closed under multiplication and addition, 2k^2 + k is a natural number.

## Lemma L3 · fact 5b5cf4f3d4ade698

**Uses:** none.

**Statement.**

Let k be a natural number (where natural numbers are 0, 1, 2, ...). Set n = 2k + 1. Then n^2 + n = 2(2k^2 + 3k + 1), and 2k^2 + 3k + 1 is a natural number.

**Proof.**

Compute using distributivity and commutativity of natural number arithmetic:
n^2 = (2k + 1)^2 = (2k)^2 + 2·(2k)·1 + 1^2 = 4k^2 + 4k + 1.
Therefore n^2 + n = 4k^2 + 4k + 1 + (2k + 1) = 4k^2 + 6k + 2 = 2(2k^2 + 3k + 1).
Since natural numbers are closed under multiplication and addition, 2k^2 + 3k + 1 is a natural number.

## Main theorem · fact 944c01fafffbe05d

**Uses:** Lemma L1 (fact 95f39a5498b2ebea), Lemma L2 (fact 941ea68438409973), Lemma L3 (fact 5b5cf4f3d4ade698).

**Statement.**

Prove that for every natural number n, n^2 + n is even.

**Proof.**

Cited facts: L1 = fact 95f39a5498b2ebea; L2 = fact 941ea68438409973; L3 = fact 5b5cf4f3d4ade698.

We take natural numbers to be 0, 1, 2, ..., and we define: a natural number m is even if there exists a natural number t with m = 2t. Let n be an arbitrary natural number.
By L1: for every natural number n there exists a natural number k with n = 2k or n = 2k + 1. Fix such a k for our n and split into the two cases.
Case 1: n = 2k. By L2: n^2 + n = 2(2k^2 + k) with 2k^2 + k a natural number. Setting t = 2k^2 + k, we have n^2 + n = 2t, so n^2 + n is even by the definition.
Case 2: n = 2k + 1. By L3: n^2 + n = 2(2k^2 + 3k + 1) with 2k^2 + 3k + 1 a natural number. Setting t = 2k^2 + 3k + 1, we have n^2 + n = 2t, so n^2 + n is even by the definition.
Since L1 guarantees one of the two cases holds, in all cases n^2 + n is even. Since n was arbitrary, for every natural number n, n^2 + n is even.
