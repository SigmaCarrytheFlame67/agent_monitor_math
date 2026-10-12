---
title: Problem sources and status
description: Where the Ansätze problems come from, how each is categorized as solved, AI proposed, partial or open, how status stays current, and how sources are credited.
---

# Where the problems come from and how their status is decided

The [problem catalogue](/problems) gathers established problem collections and named lists. Every entry has exactly one **category** (Solved, AI proposed, Partial or Open) and, when it is not open, the primary evidence for it. Categories come from the sources that maintain each problem, from research in primary sources, and from reviewed operator decisions. A model's judgement is never a certificate: "AI proposed" is never counted as solved, and Lean results published upstream are labelled "not re-checked by this server".

| Collection | Entries | Source and revision |
| --- | --- | --- |
| Erdős Problems | every problem in the database, in every state | [teorth/erdosproblems](https://github.com/teorth/erdosproblems) `data/problems.yaml` at [`48e0c21`](https://github.com/teorth/erdosproblems/tree/48e0c21bd250e6e725e8a1fca31acabe542dbeb1) (October 11, 2026) |
| Kourovka Notebook | every problem in the main section | No. 21, [arXiv:1401.0300v48](https://arxiv.org/abs/1401.0300v48) (October 6, 2026); the solved archive is excluded |
| Millennium Prize Problems | all seven | [Clay Millennium page](https://www.claymath.org/millennium-problems/), checked October 11, 2026 |
| Hilbert’s problems and open extensions | the 23 problems of 1900 and two open extensions | Hilbert’s address; status researched October 2026 |
| Smale’s problems | the 18 problems of 1998 | Smale’s list; status researched October 2026 |
| Ramanujan’s legacy | three questions | Reviewed literature (see below) |
| Wikipedia: unsolved problems | list entries that are not already in a collection above | [List of unsolved problems in mathematics](https://en.wikipedia.org/w/index.php?title=List_of_unsolved_problems_in_mathematics&oldid=1379514120), revision 1379514120 |
| Recently solved (Wikipedia list) | “Problems solved since 2015” entries not already present | the same revision |

Hilbert, Smale and the two Wikipedia collections list an entry once its research record exists; the counts on the Problems page are authoritative. Source facts (states, markers, pages, dates, links and revision ids) are pinned in `agent_monitor/data/problem_sources/`; no problem text from the Kourovka Notebook, erdosproblems.com or Wikipedia is reproduced.

## Categories

Each entry has one category, checked in this order. Withdrawn or refuted claims never count.

| Category | Meaning | Rules |
| --- | --- | --- |
| **Solved** | Answered, and the answer is accepted (source of record, refereed publication, or broad expert acceptance). | **S1** the source that maintains the problem lists it as answered: teorth state proved, disproved, solved or independent; a Kourovka ∗ before the problem number; the Clay *Solved problems* listing. **S2** a refereed or broadly accepted answer. |
| **AI proposed** | An AI system produced a claimed full solution that mathematicians have not yet confirmed. Treated as unsolved until confirmed. | **A1** the maintainers flag the whole problem (a Kourovka ♯ before the number: “some AI system proposed a solution that has not yet been confirmed by mathematicians”). **A2** a documented AI-produced claim to answer the whole problem, not yet confirmed. |
| **Partial** | An explicitly stated part of the problem is answered; the rest remains open. | **P1** a Kourovka ∗ on a sub-item such as “∗b)”. **P2** an accepted answer to an explicitly stated part. |
| **Open** | No accepted answer. Partial results, human claims under review, and AI partial results are shown as badges. | **O** everything else. |

Classical special cases of a single-statement problem do not make it partial: the Riemann hypothesis stays open even though positive proportions of zeros are known to lie on the critical line.

**Badges** add context without changing the category: *AI partial result*, *AI solution confirmed*, *AI-assisted solution*, *Claim under review*, *Lean proof awaiting review*, *Withdrawn claim*, *Disputed*, *Sources disagree*, *Prize*, *Ambiguous statement*, *Not in current edition*, *Lean statement*, the source’s own sub-status (for example *Falsifiable*), and *New report (unreviewed)* for live signals.

### How a category is decided

1. **Operator decision.** A committed decision (see [Review](#review-and-decisions)) overrides everything else. Each decision names who made it, and the entry shows it with the reason.
2. **Source of record.** Deterministic parsers read the teorth state, the Kourovka markers and the Clay listing.
3. **Research.** A researched record for the entry applies only when it is included, not marked uncertain, and backed by evidence. Research can mark an entry solved only with a full answer that is refereed, accepted by the source’s maintainers, or broadly accepted, and partial only with such an answer to an explicitly stated part (a special case does not count). A problem whose stated parts are answered by separate results becomes solved through a reviewed decision. Research cannot assert a source marker: a researched S1, A1 or P1 that the parsed source does not show is read as S2, A2 or P2, must meet that rule’s evidence, and is flagged for review; a “source marker” it cites outside the parsed sources of record counts as an ordinary citation. If research says solved while the source of record still lists the problem as open, the entry carries *Sources disagree* and is flagged for review.
4. **Reviewed curation** for the few entries without another basis (Navier–Stokes).
5. Otherwise **Open**.

**Only an answer counts.** Evidence supports Solved or Partial only when it is an answer: a listing that says a problem is open, or an accepted formal *statement*, never does. **Formal proofs** are strong evidence but not, on their own, acceptance: a Lean proof that the formal-conjectures maintainers accepted against their statement makes a problem Solved (S2) when the answer also has independent confirmation (a second independent proof or formalization, or broad expert acceptance). An AI-produced proof checked only by its own formalization stays AI proposed, with *Lean proof awaiting review*; a human proof in the same position stays Open with *Claim under review*. Who originated a proof decides the actor: a paper whose authors state that they originated the ideas and used AI tools is a human claim, not an AI proposal.

Every evidence item records its kind (accepted result, claim, partial result, withdrawal, refutation, source marker, formalization, survey), actor (human, AI, AI-assisted, maintainer), scope (full, part, special case, related, variant), outcome, date and precision, an HTTPS link, and an acceptance level (refereed, accepted by the maintainer, broadly accepted, preprint, announced, Lean-checked but unreviewed, withdrawn, refuted, disputed). Lean status is judged per claim, never from a family-level flag. A manuscript that openai/math has withdrawn never counts, whoever cites it: its evidence is marked withdrawn and the entry gets *Withdrawn claim*.

**Change dates.** An entry whose category rests on dated answers or claims (research, reviewed curation or a decision) records when the first of them appeared (`status_changed`, only as precise as its sources); if one of them is undated, no date is recorded. A category from a source marker records none: a marker is dated by the edition or database update that carries it, not by when it appeared. The Kourovka markers new since the September 2026 edition (arXiv v46) arrived with the editors’ October updates of September 30 (v47) or October 6 (v48), and the pinned facts cannot tell which. Changes found by the live overlay carry the time they were found.

**Manuscripts.** Each answer or claim also says whether its link is a manuscript about the entry (`manuscript`): an arXiv paper, a GitHub repository, folder or file, or a direct PDF, TeX or archive file, linked as the answer for at most ten entries. Indexes (wikis, problem lists, statement collections, openai/math outside a manuscript’s folder), compilations linked for more entries, and landing pages are not. *Analyze the claimed solution* is offered only for a manuscript.

## Erdős Problems

The [maintainers’ database](https://github.com/teorth/erdosproblems) by Thomas Bloom, Terence Tao and contributors is the source of record; erdosproblems.com no longer shows statuses. All states are imported:

- **proved, disproved, solved, independent** → Solved (S1), linked to the database line at the pinned commit.
- **falsifiable, verifiable, decidable, not provable, not disprovable** are kinds of *open* problem in the database’s own definitions → Open with the sub-status badge.
- **“open (Lean)”**, a Lean solution “not yet digested by a human reader” → Open with *Lean proof awaiting review*.
- Names come from the database’s `comments` field (“Erdős #1135 · Collatz conjecture”); “ambiguous statement” becomes a badge.

Further signals: the [AI-contributions wiki](https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems) (frozen June 30, 2026; colours, systems and dates only) gives *AI partial result* for 🟡 primary results that are not variants, and *AI-assisted solution* for solved problems with a 🟢 primary result. A 🟢 result, or a “research solved” mark in [formal-conjectures](https://github.com/google-deepmind/formal-conjectures), on a problem the database lists as open gives *Sources disagree*. Lean statements link to formal-conjectures at commit `d352839`. The database metadata is [Apache-2.0](/problem-licenses/erdos-apache-2.0.txt); the website’s problem text is not reproduced.

## Kourovka Notebook

The *Kourovka Notebook: Unsolved Problems in Group Theory*, edited by E. I. Khukhro and V. D. Mazurov, is read from [arXiv:1401.0300v48](https://arxiv.org/abs/1401.0300v48) (No. 21, October 2026). Every problem of the main section is listed; each links to its page in that PDF (`#page=N`), so links stay valid when later versions appear.

The editors mark an answered problem with ∗ before its number and an answered part with ∗ before the part (“∗b)”). Since September 2026, ♯ marks “some Artificial Intelligence (AI) system proposed a solution that has not yet been confirmed by mathematicians. Such a problem should be regarded as unsolved.” Header and part markers are parsed separately; ∗ inside mathematics (G ∗ H, W∗(F_k)) is notation. A ♯ on one part only (8.54) leaves the problem open with *AI partial result*. Solutions the Notebook cites are linked; “AI solution confirmed by …” gives *AI solution confirmed*.

Kourovka 20.3 is absent from the October edition without comment (the preface says numbers go missing when problems are withdrawn at the authors’ request). Its entry is kept, open, with *Not in current edition*, linking its [September 2026 page](https://arxiv.org/pdf/1401.0300v46#page=146). Kourovka 21.93 and Erdős #274 both state the Herzog–Schönheim conjecture, so they are one entry, under Erdős #274, with both references.

Selected details cover two longstanding questions:

- **1.3, integral group-ring zero divisors:** [Linnell’s account](https://personal.math.vt.edu/linnell/research/durham.pdf) gives the right-orderable and elementary amenable cases. [Gardam’s counterexample](https://www.gilesgardam.com/papers/unit-conjecture-counterexamplev3.pdf) disproves the related unit conjecture; its group still satisfies the zero-divisor conjecture.
- **12.20, Thompson F amenability:** the group is defined in [Cannon–Floyd–Parry](https://www.imo.universite-paris-saclay.fr/~emmanuel.breuillard/Cannon.pdf). [Brin–Squier](https://pi.math.cornell.edu/~justin/Limited2Cornell/PLoI.pdf) prove the absence of nonabelian free subgroups. [Kielak’s 2016 appendix](https://arxiv.org/pdf/1605.09133v2) gives the Ore-condition reduction mentioned in the notebook’s 2026 comment. A [withdrawn non-amenability claim](https://arxiv.org/abs/1408.2188) is labeled accordingly.

The book remains copyright © E. I. Khukhro and V. D. Mazurov, 2026, distributed on arXiv under its non-exclusive licence. Only problem numbers, page numbers, markers and links are used; the two selected statements are short, original paraphrases with citations.

### ConjectureBench attribution

The canonical Kourovka index of the September 2026 catalogue was adapted from [ConjectureBench](https://github.com/bespokelabsai/conjecture-bench), by Anirudha Ramesh and Shreyas Pimpalgaonkar, Bespoke Labs (2026), revision [`357bcb1a1daf93917d42e8206ceaa55645729a09`](https://github.com/bespokelabsai/conjecture-bench/tree/357bcb1a1daf93917d42e8206ceaa55645729a09). Its curation metadata is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the upstream [data license](/problem-licenses/conjecturebench-license-data.txt) and [source notice](/problem-licenses/conjecturebench-notice.txt) are preserved. Since October 2026 the index is read directly from the Notebook’s arXiv edition.

## Millennium Prize Problems

The [Clay Mathematics Institute](https://www.claymath.org/millennium-problems/) page decides the source status: Poincaré is listed under *Solved problems* and is Solved (S1), with Perelman’s [three](https://arxiv.org/abs/math/0211159) [arXiv](https://arxiv.org/abs/math/0303109) [papers](https://arxiv.org/abs/math/0307245); the Millennium Prize was awarded in 2010 and declined.

**Navier–Stokes is AI proposed, not solved.** OpenAI’s September 2026 [announcement, manuscript and Lean source](https://github.com/openai/NavierStokesAndEuler) claim finite-time breakdown with smooth forcing (alternatives C and D of [Fefferman’s official formulation](https://www.claymath.org/wp-content/uploads/2022/06/navierstokes.pdf)); unforced global regularity is not claimed. Clay’s [September 11 response](https://www.claymath.org/news/navier-stokes-announcement/) acknowledges an apparent resolution and an evaluation in progress, and still lists the problem as active. The fidelity of the formalization is disputed in [arXiv:2610.08144](https://arxiv.org/abs/2610.08144).

The other entries link the official descriptions by [Wiles for Birch–Swinnerton-Dyer](https://www.claymath.org/wp-content/uploads/2022/05/birchswin.pdf), [Deligne for Hodge](https://www.claymath.org/wp-content/uploads/2022/06/hodge.pdf), [Cook for P versus NP](https://www.claymath.org/wp-content/uploads/2022/06/pvsnp.pdf), and [Jaffe–Witten for Yang–Mills](https://www.claymath.org/wp-content/uploads/2022/06/yangmills.pdf), plus Clay’s [Riemann hypothesis page](https://www.claymath.org/millennium/riemann-hypothesis/). Claims of special cases, such as those in [openai/math](https://github.com/openai/math), appear as badges and evidence; three Hodge-related manuscripts there were withdrawn on October 7, 2026. Official assumptions, attribution, and [prize rules](https://www.claymath.org/millennium-problems/rules/) remain authoritative.

## Hilbert’s problems and open extensions

The historical starting point is [Hilbert’s 1900 address, in its authorized English translation](https://www.aemea.org/math/Hilbert_23_Mathematical_Problems_1900.pdf). Several problems are open to interpretation; their entries carry *Ambiguous statement* and are flagged for review. Two clearly labelled extensions keep their own entries:

- **16, part II:** a uniform bound on limit cycles of planar polynomial differential equations of fixed degree. [Buzzi–Novaes](https://arxiv.org/html/2411.09594v1) explain established lower growth and refute a claimed quadratic formula. Finiteness for one vector field does not provide a uniform bound.
- **10 over the rationals:** explicitly an **open extension**. The original integer problem was settled by Davis–Putnam–Robinson–Matiyasevich. [Garcia-Fritz–Pasten–Vidaux (2025)](https://www.sciencedirect.com/science/article/pii/S0022314X25001180), with an [author preprint](https://arxiv.org/abs/2311.01958v2), prove undecidability after adding height-comparison conditions, leaving the plain rational-solvability question open.

## Smale’s problems

[Steve Smale’s 1998 list](https://www.fim.uni-passau.de/fileadmin/dokumente/fakultaeten/fim/lehrstuhl/muller/SmaleProblems1998.pdf). Problems that coincide with a Millennium problem (1, 2, 3) link to it as the same problem.

| Selected question | Primary research supporting the detail |
| --- | --- |
| 6 · Finiteness of planar central configurations | [Leandro’s 2026 formulation](https://arxiv.org/html/2507.13865v4); [Albouy–Kaloshin’s generic five-body theorem](https://annals.math.princeton.edu/wp-content/uploads/annals-v176-n1-p10-p.pdf); [Moczurad–Zgliczyński’s selected exceptional masses](https://arxiv.org/abs/2601.01165v2). These partial results do not cover every positive mass tuple and every number of bodies. |
| 7 · Near-optimal logarithmic energy on the sphere | [Armentano and coauthors](https://arxiv.org/html/2502.10152v3) classify small critical configurations and state the remaining algorithmic question. This concerns logarithmic energy, not the inverse-distance Thomson problem. |
| 9 · Strongly polynomial linear programming | [Dadush–Koh–Natura–Olver–Végh (STACS 2025)](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.STACS.2025.2) establish a sparse-matrix case. General polynomial time in input bit length does not meet the strongly polynomial requirement. |
| 11 · Density of hyperbolicity for complex polynomials | [Kozlovski–Shen–van Strien](https://annals.math.princeton.edu/wp-content/uploads/annals-v166-n1-p04.pdf) solve the real part. [MFCS 2025 research](https://drops.dagstuhl.de/storage/00lipics/lipics-vol345-mfcs2025/html/LIPIcs.MFCS.2025.79/LIPIcs.MFCS.2025.79.html) still uses the complex quadratic conjecture as an assumption. |

## Ramanujan’s legacy

These questions were posed by Srinivasa Ramanujan or arise from his mathematics. Later conjectures retain their actual authorship. In particular, Deligne solved the holomorphic Ramanujan–Petersson bound; the selected Maass-form extension is a different, open case.

| Selected question | Sources for the statement, partial results, and reported claims |
| --- | --- |
| Brocard–Ramanujan factorial squares | [Berndt–Galway (2000)](https://doi.org/10.1023/A:1009873805276); [Epstein–Glickman computation](https://github.com/jhg023/brocard); [2026 research on restricted sequences](https://arxiv.org/html/2606.28577v1); [Maiti’s revised preprint claim](https://arxiv.org/abs/2004.09256v2). Finite searches and restricted-sequence theorems do not settle all positive integers. |
| Lehmer nonvanishing for Ramanujan’s tau function | [Bosman’s verified finite range](https://arxiv.org/pdf/0710.1237); [Balakrishnan–Craig–Ono](https://doi.org/10.1016/j.jnt.2020.04.009); [2026 tau-function research](https://arxiv.org/html/2603.29970v2); [Lee’s withdrawal notice](https://arxiv.org/abs/1506.02098); [Shi–Wang–Solé’s 2025 withdrawal](https://arxiv.org/abs/2503.23498v2). The conjecture is Lehmer’s, not Ramanujan’s. |
| Ramanujan bounds for Hecke–Maass cusp forms | [Sarnak’s account and the Kim–Sarnak bound](https://publications.ias.edu/sites/default/files/FieldNotesCurrent.pdf); [Huang–Zhao’s 2026 partial results](https://arxiv.org/html/2605.09807v1); [Unterberger’s preprint claim](https://arxiv.org/abs/2001.10956v16). Later research still describes the all-primes conjecture as unsolved. |

The Maiti and Unterberger manuscripts are reported as unverified claims. The status assessment follows the cited research literature; this catalogue does not independently refute those manuscripts.

## Wikipedia’s list

Wikipedia’s [List of unsolved problems in mathematics](https://en.wikipedia.org/w/index.php?title=List_of_unsolved_problems_in_mathematics&oldid=1379514120) is a **discovery source**, pinned to revision 1379514120 (October 10, 2026). Its “unsolved” label is not used as a status, and its “solved since 2015” section is not evidence of acceptance. For each entry, research in primary sources decides whether it is the same problem as an existing entry (its name then becomes an alias of that entry and no new entry is created), and otherwise writes an original statement and records the status evidence. Entries that only group other bullets are skipped. Names, links and revision ids are used; Wikipedia text (CC BY-SA 4.0) is not reproduced.

## Research method

The October 2026 status research covered every Hilbert, Smale, Millennium and Ramanujan entry, the Kourovka and Erdős entries with AI-related claims or source conflicts, and the Wikipedia lists. It was AI-assisted deep research from primary sources (papers, maintainers’ records, preprints and repositories, fetched by our tools), followed by a separate verification pass. Each record states the category and rule, an original statement with sources, dated evidence items, badges, a confidence, and whether a person must review it. Records marked uncertain do not change a category, and research cannot mark an entry solved without accepted evidence. A separate integrity audit then checked every link in the catalogue (with replacements for dead and unpinned links), rendered every formula, re-checked a random sample of 40 researched categories and all reviewed decisions against their primary sources, and merged problems that appeared twice. The research file is `agent_monitor/data/problem_status_research.json`; the decisions, each with its reason, are in `agent_monitor/data/problem_status_decisions.json`.

## Keeping status current

The `ansatze-problems` timer refreshes the live status overlay, `/research/problem-status.json`, every six hours. The overlay never adds or removes entries.

- **Category changes** come only from deterministic source-of-record signals: a teorth state change, a Kourovka marker change in a newer arXiv version, a withdrawal in openai/math of a manuscript an entry cites (withdrawals take effect immediately: the entry’s records citing that manuscript are marked withdrawn, and *Analyze the claimed solution* moves to a claim that still stands), or a change in the Clay listing. They appear under recently changed entries. An entry whose category was set by a reviewed decision keeps it and shows *Sources disagree* instead; a decision that only adds badges or a headline does not hold changes back.
- **New reports (unreviewed)** come from arXiv, the [Palomar](https://palomar-registry.org/) registry (CC0 data), formal-conjectures commits (including Erdős files newly marked “research solved”), openai/math, linked Wikipedia articles and the News page’s problem links. They are shown with the disclosure “Matched automatically by Kimi K3 from primary sources; not reviewed; does not change the category.” A Wikipedia edit is labelled as a secondary source, and only the link to the edit is shown, not its text. A report from openai/math’s family table says when the family-to-problem link is an unreviewed candidate.
- **Wikipedia edits** are reported as edits to a secondary source, with the diff link and no article text. An edit is triaged only after it has stood for 12 hours (most vandalism is reverted sooner), and its report is withdrawn when a later edit removes the text again.
- **Kimi K3** (through the site’s sponsored broker) only matches a new item to at most five candidate entries, choosing a relation from a fixed list and quoting the item verbatim, and triages text added to linked Wikipedia articles. It cannot set a category, add links or change titles and dates. It runs at most 8 times per refresh and 48 times per day; items it could not classify wait for a later refresh.
- Each source fails closed: a source that cannot be read keeps its last good result and is listed as an error. A newer Kourovka edition is refused when many problems or markers vanish at once, or far more markers appear than an edition adds (a text-extraction change, not an editorial one). The page shows when status was last checked; a refresh counts as successful only when a source actually answered.

## Review and decisions

Operators review flagged research and new reports with `scripts/review_problem_status.py`:

```bash
python scripts/review_problem_status.py list --overlay /var/lib/ansatze-problems/problem-status.json
python scripts/review_problem_status.py show kourovka-12-020
python scripts/review_problem_status.py accept kourovka-12-020 --category ai-proposed --rule A2 \
  --evidence-url https://github.com/openai/math/... --evidence-title "..." --actor ai --acceptance preprint \
  --note "Full claim checked against the catalogued statement"
python scripts/review_problem_status.py reject 'erdos-0809|<signal key>' --note "not about this problem"
```

A decision records who made it, when, why and the evidence; *Solved* needs evidence of its own: a refereed, maintainer-accepted or broadly accepted full answer. The October 2026 decisions were made by Claude, an AI, for the site operator during the status research and its audit, and are marked as not yet signed off by a person; an operator signs one off by re-recording it with `accept … --by <name>`. Decisions live in `agent_monitor/data/problem_status_decisions.json` and take effect when the catalogue is rebuilt and published. Rejected reports disappear from the overlay at the next refresh. `show --state /var/lib/ansatze-problems/state.json` also prints the privately kept quote of a Wikipedia report.

## Licensing and attribution

| Source | Use |
| --- | --- |
| Erdős Problems database (teorth/erdosproblems) | Apache-2.0 metadata: states, dates, names, tags, links. erdosproblems.com text is not reproduced. |
| AI contributions to Erdős problems (wiki) | Facts only: colours, systems, dates, sections. |
| Kourovka Notebook (arXiv non-exclusive licence) | Problem numbers, pages, markers and links only. |
| Wikipedia | Discovery: names, links and revision ids; statements and status are original. |
| formal-conjectures (Apache-2.0) | Links to Lean statements and the maintainers’ categories. |
| openai/math (Apache-2.0) | Family and manuscript titles, dates, withdrawals and links, attributed. |
| Palomar (CC0) | Registration titles, abstracts and links for unreviewed reports. |
| Clay Mathematics Institute | Listing status and links; original short summaries. |

See also `ATTRIBUTION.md` in the repository.

## Reading an entry

A **source reference** identifies the original problem and where to read it. Most Erdős and Kourovka entries remain references with subject labels; their full statements have not been reproduced. An **editorial statement** is an original mathematical summary with linked sources. Selected entries separate established partial results, conditional or computational evidence, and reported proof claims; a preprint claim, a withdrawn argument and an accepted theorem have different labels. An empty claims list means no report was selected, not that no attempted proof exists.

Read the linked formulation for its complete assumptions. “Click to solve” opens a research attempt using the selected question and context; it does not promise a solution. A formal statement, a claimed formal proof, and a proof checked by a verifier are separate things; the catalogue does not certify its listed claims.

## Exploration-fit ratings

An exploration-fit score is an **AI-assisted editorial heuristic**, based on how concrete a research starting point the entry provides. It is not a calibrated difficulty, importance, success-probability, or proof-quality score. Imported references generally require a literature review first; broad named conjectures require narrowing to a special case. Read each entry’s reason and confidence.

## Reproducibility and updates

The public catalogue (`/research/open-problems.json`, schema 3) and the workspace copy are byte-identical. Collection boilerplate is stored once per collection (`sources[].defaults`), and `content_sha256` identifies the problems array the live overlay was computed against. To update:

```bash
# 1. Refresh the pinned source facts (local copies, or --fetch for upstream).
python scripts/refresh_problem_sources.py --fetch --only erdos,openai-math,clay
python scripts/refresh_problem_sources.py --kourovka-pdf 1401.0300v49.pdf --kourovka-version 49 --only kourovka
python scripts/refresh_problem_sources.py --formal-conjectures-dir ../formal-conjectures --only formal-conjectures
# 2. Rebuild the catalogue, its two copies and the bundled overlay (deterministic).
python scripts/build_problem_catalogue.py
python scripts/build_problem_catalogue.py --check
```

A refresh that loses more than 2% of a source’s records keeps the previous file; for the Kourovka Notebook it also refuses when previously listed problems or markers vanish in bulk, markers appear in bulk, or parse anomalies grow, and openai/math withdrawals that match no manuscript of the previous revision are listed under `unmatched_withdrawals` for review (pass `--openai-previous-commit` to compare with an intermediate revision). After reviewing a genuine change, `--force` replaces the file. The build refuses to drop any published problem id, because forum threads, DAG memory and the solve hand-off store ids. Run `tests/test_open_problem_catalogue.py`, `tests/test_problem_catalogue_build.py`, `tests/test_problem_sources.py` and `node --test tests/test_problem_catalogue.mjs` before publishing.

### Historical imports

The earlier 8,052-entry selection included Linear Code Tables, Constant-Weight Codes, Covering Designs, and Ramsey Numbers. Those collections are no longer selected. Their adapters and provenance remain in `scripts/problem_expansion.py`. The September 2026 importer (`scripts/import_open_problems.py`) and curator are retired: the September Kourovka PDF they pinned is no longer published.

Historical numeric data remain attributable to [Markus Grassl and contributors](https://codetables.de/), [A. E. Brouwer and contributors](https://www.win.tue.nl/~aeb/codes/Andw.html), [Daniel M. Gordon and covering-design contributors](https://dmgordon.org/covering-designs/), and the [Leaps in Bounds tracker and its cited researchers](https://leapsinbounds.org/). Retaining these records does not imply that their bounds or open status were rechecked for the current selection.

## Community contributions

Anyone can read the forum. Signed-in users can propose a problem, start a discussion, or reply. Proposals remain **unreviewed community submissions** until reviewed; they do not automatically enter the sourced catalogue or harness memory. Include the statement, original source, and what remains open. Discussions can also report a correction or a new result.
