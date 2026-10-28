---
layout: home
title: Ansätze
titleTemplate: An AI workspace for mathematical research

hero:
  name: Ansätze
  text: Every proof begins with an idea.
  tagline: An AI workspace for mathematical research at ansatze.ai. By default, each run plans, proves, and independently checks its lemmas with the Ansatz CGM pipeline, then shows the evidence as graphs you can inspect and a Lean stage you can run.
  actions:
    - theme: brand
      text: Use as guest
      link: /guest
      target: _self
    - theme: alt
      text: Sign in
      link: /sign-in
      target: _self
    - theme: alt
      text: Quickstart
      link: /quickstart
---

<script setup>
import AtlasPreview from './.vitepress/theme/AtlasPreview.vue'
import HomeProblemCollections from './.vitepress/theme/HomeProblemCollections.vue'
</script>

<aside class="ansatz-definition" aria-label="Meaning of Ansätze"><span>THE NAME</span><p><strong>Ansätze</strong> <em>plural noun · mathematics</em> Proposed forms or starting approaches to a problem. The work begins by asking where they lead.</p></aside>

<AtlasPreview />

<HomeProblemCollections />

<section class="ansatz-system" aria-labelledby="system-title">
  <header class="ansatz-section-heading">
    <p class="ansatz-section-index">01 / THE SYSTEM</p>
    <h2 id="system-title">Built to show its work.</h2>
    <p>Ansätze keeps the question, the search process, and the resulting evidence together—so a promising answer is the start of inspection, not the end of it.</p>
  </header>

  <div class="ansatz-capability-list">
    <article><span>01</span><div><h3>A proving pipeline, not one answer</h3><p>The default Ansatz CGM pipeline has a strategist propose plans, workers write lemmas and proofs, an independent verifier check each lemma in a fresh call, and a curator score what remains open.</p></div></article>
    <article><span>02</span><div><h3>Two graphs for each CGM run</h3><p><strong>Facts</strong> shows the lemmas the verifier accepted and what each one depends on. <strong>Explore</strong> shows the search itself: plans, attempts, verdicts, obstacles, and dead ends.</p></div></article>
    <article><span>03</span><div><h3>A formal Lean stage</h3><p>Formalize the result in Lean 4, then compile, check, and audit the statement. A model verifier’s acceptance is never presented as a Lean certificate.</p></div></article>
    <article><span>04</span><div><h3>Continue, don’t restart</h3><p>Signed-in accounts can add feedback to an existing run and continue from its workspace instead of rebuilding context.</p></div></article>
    <article><span>05</span><div><h3>Compare approaches</h3><p>Run the same CGM pipeline on your own API key (accounts with full access can also use the Codex or Claude Code subscription they linked), or try other proving engines with a common view of progress and cost.</p></div></article>
    <article><span>06</span><div><h3>Preserve useful work</h3><p>Keep proofs, graphs, Lean sources, and research memory attached to the run that produced them.</p></div></article>
  </div>
</section>

<section class="ansatz-workflow" aria-labelledby="workflow-title">
  <header class="ansatz-section-heading ansatz-section-heading-compact">
    <p class="ansatz-section-index">02 / WORKFLOW</p>
    <h2 id="workflow-title">From question to inspectable result.</h2>
  </header>

  <ol class="ansatz-steps">
    <li class="ansatz-step"><span>01</span><div><strong>Describe the problem</strong><p>Paste a statement or choose one from <a href="/problems">Problems</a>. Guests start with Ansatz CGM on Kimi K3; no key or account is needed when hosted access is available.</p></div></li>
    <li class="ansatz-step"><span>02</span><div><strong>Watch the pipeline</strong><p>Follow the strategist, workers, independent verifier, and curator as the run progresses.</p></div></li>
    <li class="ansatz-step"><span>03</span><div><strong>Inspect Facts and Explore</strong><p>Open the <b>DAG</b> tab to see which lemmas the verifier accepted, how they combine into the target, and which routes stalled.</p></div></li>
    <li class="ansatz-step"><span>04</span><div><strong>Check it in Lean</strong><p>The formal stage translates the proof into Lean 4. Read its status literally: compiled, verified, incomplete (sorry), wrong statement, or failed.</p></div></li>
  </ol>
</section>

<p class="ansatz-attribution">The Ansatz CGM pipeline adapts prompts and data contracts from the Ansatz <em>continual-graph-memory</em> project by FrenzyMath (Apache License 2.0). Read <a href="/quickstart#how-a-cgm-run-works">how a CGM run works</a>.</p>

<section class="ansatz-next" aria-labelledby="next-title">
  <p class="ansatz-section-index">03 / START HERE</p>
  <h2 id="next-title">Take a closer look.</h2>
  <p>Try the console in a browser-scoped guest session, sign in for durable account history, or read the main concepts first.</p>
  <nav aria-label="Getting started links">
    <a href="/guest">Use as guest <span aria-hidden="true">→</span></a>
    <a href="/sign-in">Sign in <span aria-hidden="true">→</span></a>
    <a href="/news">News <span aria-hidden="true">→</span></a>
    <a href="/quickstart">Quickstart <span aria-hidden="true">→</span></a>
  </nav>
</section>
