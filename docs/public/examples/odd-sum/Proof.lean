import Mathlib

/- Sum of two odd integers is even: statement skeleton. -/

/-- For every integer x: (a) x is odd iff there exists k ∈ ℤ with x = 2k + 1,
and (b) x is even iff there exists t ∈ ℤ with x = 2t. These are the exact
parametrizations adopted throughout the write-up, and in Mathlib they coincide
with the definitions of `Odd` and `Even` on `ℤ`.
Informal: 4fd6fa17f43b8308
Cites: self-derived — definitional unpacking of `Odd` and `Even` on `ℤ` -/
lemma odd_even_param (x : ℤ) :
    (Odd x ↔ ∃ k : ℤ, x = 2 * k + 1) ∧ (Even x ↔ ∃ t : ℤ, x = 2 * t) := by
  constructor
  · constructor
    · rintro ⟨k, rfl⟩
      exact ⟨k, rfl⟩
    · rintro ⟨k, rfl⟩
      exact ⟨k, rfl⟩
  · constructor
    · rintro ⟨t, rfl⟩
      exact ⟨t, by ring⟩
    · rintro ⟨t, rfl⟩
      exact ⟨t, by ring⟩

/-- The integers are closed under addition: for all m, n ∈ ℤ, m + n is an
integer, and consequently m + n + 1 is an integer.
Informal: 62018a4d2785e13b
Cites: self-derived — `ℤ` is a ring, hence closed under addition -/
lemma int_add_closure (m n : ℤ) :
    (∃ s : ℤ, s = m + n) ∧ (∃ t : ℤ, t = m + n + 1) := by
  exact ⟨⟨m + n, rfl⟩, ⟨m + n + 1, rfl⟩⟩

/-- For all integers a, b ∈ ℤ, if a is odd and b is odd then a + b is even,
where odd means ∃ k, x = 2k + 1 and even means ∃ t, y = 2t.
Informal: 7375ef9adbaef834
Cites: self-derived — expansion a + b = (2m + 1) + (2n + 1) = 2(m + n + 1);
`odd_even_param` — provides the parametrizations;
`int_add_closure` — witnesses m + n + 1 ∈ ℤ -/
lemma odd_add_odd_even (a b : ℤ) (ha : Odd a) (hb : Odd b) : Even (a + b) := by
  -- Parametrize a and b via the odd characterization from `odd_even_param`.
  obtain ⟨m, hm⟩ := (odd_even_param a).1.mp ha
  obtain ⟨n, hn⟩ := (odd_even_param b).1.mp hb
  -- By `int_add_closure`, t = m + n + 1 is an integer; it is the witness.
  obtain ⟨t, ht⟩ := (int_add_closure m n).2
  rw [hm, hn]
  -- Exhibit q = t with a + b = 2q, as in `odd_even_param` (b).
  exact (odd_even_param ((2 * m + 1) + (2 * n + 1))).2.mpr ⟨t, by rw [ht]; ring⟩

/-- The sum of two odd integers is even.
Informal: 7ac28a5a823dd9a4
Cites: self-derived — two odd integers sum to an even integer;
`odd_even_param` — odd iff ∃ k, x = 2k + 1 and even iff ∃ t, y = 2t;
`int_add_closure` — closure of ℤ under addition;
`odd_add_odd_even` — the odd + odd = even lemma -/
theorem main (a b : ℤ) (ha : Odd a) (hb : Odd b) : Even (a + b) := by
  exact odd_add_odd_even a b ha hb
