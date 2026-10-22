# Proof

This proof is the verified fact closure of the target theorem. Each lemma and the main theorem were accepted one at a time by an independent, response-only model verifier that saw only that statement, its proof, and the statements of the facts it cites. Model-verifier acceptance is not a Lean or other formal certificate.

## Lemma 1 · fact 7421315917b97fe9

**Uses:** none.

**Statement.**

Let k ≥ 1 be an integer and let p_1, ..., p_k be prime numbers. Let P = p_1 p_2 ⋯ p_k and N = 4P − 1. Then:
- N > 1;
- N ≡ 3 (mod 4);
- for every i ∈ {1, ..., k}, p_i does not divide N.

**Proof.**

Each p_i is a prime, so p_i ≥ 2. Since k ≥ 1, P is a product of k ≥ 1 integers each ≥ 2, so P ≥ 2.

N > 1: from P ≥ 2 we get N = 4P − 1 ≥ 7 > 1.

N ≡ 3 (mod 4): N = 4P − 1 = 4(P − 1) + 3 with P − 1 an integer.

No p_i divides N: fix i ∈ {1, ..., k}.
- p_i divides P, since P = p_i · ∏_{j≠i} p_j, so p_i divides 4P.
- If p_i also divided N, it would divide 4P − N = 1.
- That is impossible, because p_i ≥ 2 > 1.

## Lemma 2 · fact 9fcb0623f06180fa

**Uses:** none.

**Statement.**

For all integers a and b with a ≡ 1 (mod 4) and b ≡ 1 (mod 4), we have ab ≡ 1 (mod 4).

**Proof.**

By hypothesis there exist integers s and t with a = 4s + 1 and b = 4t + 1. Then
ab = 16st + 4s + 4t + 1 = 4(4st + s + t) + 1.
Here 4st + s + t is an integer, so ab ≡ 1 (mod 4).

## Lemma 3 · fact 6c1eb1182fce9054

**Uses:** Lemma 2 (fact 9fcb0623f06180fa).

**Statement.**

For every integer N with N > 1 and N ≡ 3 (mod 4), there exists a prime number q such that q divides N and q ≡ 3 (mod 4).

**Proof.**

Cited facts: L1 = fact 9fcb0623f06180fa.

Let C be the set of all integers N such that N > 1, N ≡ 3 (mod 4), and no prime q with q ≡ 3 (mod 4) divides N. We must show C is empty.

Suppose, for contradiction, that C is nonempty. C is a nonempty set of positive integers, so by the well-ordering principle it has a least element; call it N.

Step 1: N is odd. Since N ≡ 3 (mod 4), there is an integer m with N = 4m + 3 = 2(2m + 1) + 1. Hence 2 does not divide N.

Step 2: N is not prime. If N were prime, then q := N would be a prime with q ≡ 3 (mod 4) and q dividing N. That contradicts N ∈ C.

Step 3: N factors into two smaller factors. N > 1 and N is not prime. So there is an integer a with 1 < a < N and a dividing N. Put b := N / a, which is a positive integer. From a < N we get b = N/a > 1. From a > 1 we get b = N/a < N. So 1 < a < N, 1 < b < N, and N = ab.

Step 4: a and b are each ≡ 1 or 3 (mod 4).
- If 2 divided a, then 2 would divide ab = N, contradicting Step 1. So a is odd, and likewise b is odd.
- Every integer x has a remainder r ∈ {0, 1, 2, 3} with x ≡ r (mod 4).
- If r ∈ {0, 2}, then x = 4u + r = 2(2u + r/2) is even.
- Hence each odd integer is ≡ 1 or ≡ 3 (mod 4). This applies to both a and b.

Step 5: one of a, b is ≡ 3 (mod 4).
- Suppose a ≡ 1 (mod 4) and b ≡ 1 (mod 4).
- By L1 (for all integers a, b with a ≡ 1 (mod 4) and b ≡ 1 (mod 4), ab ≡ 1 (mod 4)), we get N = ab ≡ 1 (mod 4).
- This contradicts N ≡ 3 (mod 4), since 1 ≢ 3 (mod 4).
- So by Step 4, at least one of a, b is ≡ 3 (mod 4). Let c denote such a factor, so c ∈ {a, b}, c ≡ 3 (mod 4), 1 < c < N, and c divides N.

Step 6: contradiction with minimality.
- Since c > 1, c ≡ 3 (mod 4) and c < N, minimality of N gives c ∉ C.
- By the definition of C, there is therefore a prime q with q ≡ 3 (mod 4) and q dividing c.
- Since c divides N, q divides N. This contradicts N ∈ C.

Hence C is empty, which is the claim.

## Main theorem · fact d967a1257629f8bc

**Uses:** Lemma 1 (fact 7421315917b97fe9), Lemma 3 (fact 6c1eb1182fce9054).

**Statement.**

Prove that there are infinitely many prime numbers p with p ≡ 3 (mod 4).

**Proof.**

Cited facts: L2 = fact 6c1eb1182fce9054; L3 = fact 7421315917b97fe9.

Let S be the set of prime numbers p with p ≡ 3 (mod 4). The number 3 is prime and 3 = 4·0 + 3, so 3 ∈ S and S is nonempty.

Suppose, for contradiction, that S is finite. Since S is nonempty, we may list its elements as p_1, ..., p_k with k ≥ 1. Put P = p_1 ⋯ p_k and N = 4P − 1.

By L3 (for k ≥ 1 primes p_1, ..., p_k with P = p_1⋯p_k and N = 4P − 1: N > 1, N ≡ 3 (mod 4), and no p_i divides N), we have N > 1, N ≡ 3 (mod 4), and no p_i divides N.

By L2 (every integer N > 1 with N ≡ 3 (mod 4) has a prime divisor q with q ≡ 3 (mod 4)), there is a prime q with q ≡ 3 (mod 4) and q dividing N.

Then q ∈ S, so q = p_j for some j ∈ {1, ..., k}. Hence p_j divides N, which contradicts L3. Therefore S is infinite: there are infinitely many primes p with p ≡ 3 (mod 4).
