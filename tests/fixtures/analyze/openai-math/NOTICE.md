# Notice: openai/math test fixtures (Apache-2.0)

The TeX sources in this folder are unmodified copies of manuscript sources from
[github.com/openai/math](https://github.com/openai/math) at commit
`fd4aeeb2ee4fc729c18d98444fed42fd0529eeeb`, licensed under the Apache License,
Version 2.0 (a copy is in `LICENSE`). Authorship as stated upstream: OpenAI.

| Folder | Upstream path (at the commit above) |
| --- | --- |
| `irrationality-exponent-of-pi/` | `preprints/The-irrationality-exponent-of-pi-is-2-September-24-2026/build/` |
| `kaplansky-char2/` | `preprints/A-Counterexample-to-Kaplanskys-Direct-Finiteness-Conjecture-in-Characteristic-Two-September-23-2026/build/` |
| `potts-capacity/` | `preprints/A-Capacity-Criterion-for-Four-State-Potts-Reconstruction-on-Trees-October-5-2026/build/` |

PDFs and READMEs are not included. The files are used only as parser test
fixtures for the Analyze mode (`tests/test_analyze_latex.py`); they are never
compiled or executed. `expected_prototype.json` records the statements and
edges that the math-kg-tools evaluation prototype extracted from the same
sources, for parity checks.
