<!-- Show one part of the Quickstart's worked example, built from a real guest run's saved artifacts. -->
<script setup>
import { computed, ref } from 'vue'
import { withBase } from 'vitepress'
import run from './quickstartExample.json'
import { renderMath } from './mathText.js'
import './vendor/katex/katex.min.css'

const props = defineProps({
  part: { type: String, required: true, validator: value => ['problem', 'proof', 'facts', 'explore', 'lean'].includes(value) },
})

const math = text => renderMath(text || '', { keepTogether: true })
const byId = Object.fromEntries(run.lemmas.map(lemma => [lemma.id, lemma]))
const target = run.lemmas.find(lemma => lemma.label === 'Target theorem')
const selectedId = ref(run.lemmas[2].id)
const selected = computed(() => byId[selectedId.value])
const minutes = seconds => seconds >= 90 ? `${Math.round(seconds / 60)} min` : `${seconds} s`
const thousands = value => (value / 1000).toFixed(1) + 'k'
const date = new Date(run.captured + 'T12:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
const counts = run.explore.counts
const list = items => items.length < 2 ? items.join('') : items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1]
const graphSummary = [
  `${counts.plan} plans`, `${counts.goal - 1} subgoals`, `${counts.proof_attempt} worker attempts`, `${counts.verification} verifier verdicts`,
  `${counts.fact} facts`, `${counts.obstacle} obstacles (pitfalls the strategist flagged)`, `${counts.conclusion} unverified conclusions`,
]
const closedBy = run.explore.plans.find(plan => plan.rule === 'accepted-target')
// Why each plan ended where it did, from the curator's deterministic rules and notes.
function why(plan) {
  if (plan.rule === 'accepted-target') {
    const open = plan.subgoals.filter(goal => goal.status === 'open')
    const scores = [...new Set(open.map(goal => goal.score))]
    return 'Closed because this worker’s proof of the target was accepted; a plan closes that way even with subgoals still open.'
      + (open.length ? ` The curator left ${list(open.map(goal => goal.label))} open${scores.length === 1 && scores[0] != null ? ` (score ${scores[0]})` : ''}, noting ${open.length > 1 ? 'both steps are' : 'the step is'} already inside that accepted proof.` : '')
  }
  if (plan.rule === 'dead-member')
    return `Dead: ${closedBy ? closedBy.label + ' had already proved the target and ' : ''}this attempt was never verified. The curator marked its subgoals dead, and a plan with a dead subgoal is dead.`
  return ''
}
const verdictName = name => name === 'THEOREM' ? 'Theorem' : name
const sha = run.lean.source_sha256
const checks = run.lean.checks
</script>

<template>
  <div class="ex">
    <!-- The problem -->
    <div v-if="props.part === 'problem'" class="ex-card ex-problem">
      <p class="ex-eyebrow">The prompt, exactly as typed</p>
      <p class="ex-prompt">{{ run.problem }}</p>
      <dl class="ex-meta">
        <div><dt>Engine</dt><dd>{{ run.engine }}</dd></div>
        <div><dt>Run</dt><dd>{{ run.session }}, {{ date }}</dd></div>
        <div><dt>Proof</dt><dd>{{ minutes(run.stats.informal_seconds) }} · {{ thousands(run.stats.input_tokens) }} in / {{ thousands(run.stats.output_tokens) }} out tokens</dd></div>
        <div><dt>Lean stage</dt><dd>{{ minutes(run.stats.lean_seconds) }} · verified</dd></div>
      </dl>
    </div>

    <!-- The proof -->
    <div v-else-if="props.part === 'proof'" class="ex-proof">
      <p class="ex-basis"><span class="ex-chip ex-chip-model">Model-verifier accepted</span>Each statement was checked on its own by a fresh verifier call.</p>
      <article v-for="lemma in run.lemmas" :key="lemma.id" class="ex-card ex-lemma" :class="{ 'ex-lemma-target': lemma === target }">
        <header>
          <strong>{{ lemma.label }}</strong>
          <span v-if="lemma.uses.length" class="ex-uses">uses {{ lemma.uses.map(label => label.replace('Lemma ', 'L')).join(', ') }}</span>
          <span class="ex-chip ex-chip-model">verifier: {{ lemma.verifier.verdict }}</span>
        </header>
        <div class="ex-math" v-html="math(lemma.statement_tex)"></div>
        <details :open="lemma.label === 'Lemma 3'">
          <summary>Proof</summary>
          <div class="ex-math ex-proof-text" v-html="math(lemma.proof_tex)"></div>
        </details>
      </article>
    </div>

    <!-- Facts graph -->
    <div v-else-if="props.part === 'facts'" class="ex-facts">
      <div class="ex-dag" role="group" aria-label="Facts graph: Lemma 1 and Lemma 2 support Lemma 3; all three support the target theorem. Select a node to read it.">
        <svg class="ex-dag-edges" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <path d="M24 24 C24 31 44 30 44 38" />
          <path d="M76 24 C76 31 56 30 56 38" />
          <path d="M50 62 C50 68 50 70 50 76" />
          <path d="M5 24 L5 64 C5 71 30 68 30 76" />
          <path d="M95 24 L95 64 C95 71 70 68 70 76" />
        </svg>
        <i class="ex-arrow" style="left:44%;top:38%"></i>
        <i class="ex-arrow" style="left:56%;top:38%"></i>
        <i class="ex-arrow" style="left:50%;top:76%"></i>
        <i class="ex-arrow" style="left:30%;top:76%"></i>
        <i class="ex-arrow" style="left:70%;top:76%"></i>
        <button
          v-for="(lemma, i) in run.lemmas"
          :key="lemma.id"
          type="button"
          class="ex-node"
          :class="['ex-node-' + i, { 'ex-node-target': lemma === target }]"
          :aria-pressed="selectedId === lemma.id"
          @click="selectedId = lemma.id"
        >
          <span class="ex-node-head"><b>{{ lemma.label }}</b><span class="ex-tag"><span aria-hidden="true">✓ verifier</span><span class="ex-sr">, accepted by the model verifier</span></span></span>
          <span class="ex-node-title" v-html="math(lemma.short)"></span>
        </button>
      </div>
      <div class="ex-card ex-detail" aria-live="polite">
        <p class="ex-detail-head">
          <strong>{{ selected.label }}</strong>
          <span>{{ selected.uses.length ? 'uses ' + selected.uses.join(', ') : 'uses no other lemma' }}</span>
        </p>
        <div class="ex-math" v-html="math(selected.statement_tex)"></div>
        <p class="ex-verifier"><span class="ex-chip ex-chip-model">verifier: {{ selected.verifier.verdict }}</span> <span v-html="math(selected.verifier.summary)"></span></p>
        <p class="ex-lean-line">
          <span class="ex-chip ex-chip-kernel">Lean</span>
          <code>{{ selected.lean_decl }}</code>
          <template v-if="selected === target"> is the main theorem: kernel-checked, and its statement was audited against the problem.</template>
          <template v-else> is in the kernel-checked file. The statement audit covered the main theorem only.</template>
        </p>
      </div>
    </div>

    <!-- Explore graph -->
    <div v-else-if="props.part === 'explore'" class="ex-explore">
      <p class="ex-root"><span class="ex-status ex-status-closed">closed</span>Root problem: {{ run.problem }}</p>
      <div class="ex-lanes">
        <article v-for="plan in run.explore.plans" :key="plan.label" class="ex-card ex-lane" :class="'ex-lane-' + plan.status">
          <header>
            <strong>{{ plan.label }} · {{ plan.title }}</strong>
            <span class="ex-status" :class="'ex-status-' + plan.status">{{ plan.status }}</span>
          </header>
          <p class="ex-approach" v-html="math(plan.approach)"></p>
          <p class="ex-row-label">Subgoals (AND group)</p>
          <ul class="ex-goals">
            <li v-for="goal in plan.subgoals" :key="goal.label" :class="'ex-goal-' + goal.status">
              <b>{{ goal.label }}</b><span>{{ goal.status }}</span>
            </li>
          </ul>
          <p class="ex-row-label">Worker</p>
          <p class="ex-attempt">{{ plan.attempt.label.replace('Proof attempt', 'Attempt') }}: <b>{{ plan.attempt.result }}</b></p>
          <template v-if="plan.verdicts.length">
            <p class="ex-row-label">Verifier</p>
            <ul class="ex-verdicts">
              <li v-for="[name, verdict] in plan.verdicts" :key="name">{{ verdictName(name) }} <b>{{ verdict === 'correct' ? '✓' : verdict }}</b></li>
            </ul>
            <p class="ex-promoted">→ {{ plan.facts }} facts</p>
          </template>
          <p v-else class="ex-promoted ex-muted">No verdicts; nothing promoted to Facts.</p>
          <p v-if="why(plan)" class="ex-why">{{ why(plan) }}</p>
        </article>
      </div>
      <p class="ex-counts">The full graph has {{ run.explore.nodes }} nodes and {{ run.explore.edges }} edges: {{ list(graphSummary) }}.</p>
    </div>

    <!-- Lean 4 -->
    <div v-else-if="props.part === 'lean'" class="ex-card ex-lean">
      <p class="ex-lean-status"><span class="ex-chip ex-chip-kernel">Verified</span>Lean’s kernel accepted the file below: no errors, no <code>sorry</code>.</p>
      <dl class="ex-meta ex-lean-meta">
        <div class="ex-wide"><dt>Main theorem</dt><dd><code>{{ run.lean.signature }}</code></dd></div>
        <div><dt>Checker</dt><dd>{{ run.lean.toolchain }} · isolated sandbox</dd></div>
        <div><dt>Checks</dt><dd><template v-for="(check, i) in checks" :key="check.action">{{ i ? ' → ' : '' }}{{ check.action === 'statement' ? 'statement' : 'proof' }} {{ check.seconds }} s, {{ check.sorry ? check.sorry + ' sorry placeholders' : 'no sorry' }}</template></dd></div>
        <div><dt>Axioms used</dt><dd>{{ run.lean.axioms.join(', ') }} (standard)</dd></div>
        <div><dt>Coverage</dt><dd>{{ run.lean.coverage.verified }} / {{ run.lean.coverage.total }} facts linked to declarations in this file</dd></div>
        <div class="ex-wide"><dt>Statement audit (a model check, main theorem)</dt><dd>{{ run.lean.audit.faithful ? 'Faithful' : 'Flagged' }}: “{{ run.lean.audit.verdict }}”</dd></div>
        <div class="ex-wide"><dt>SHA-256</dt><dd><code class="ex-sha">{{ sha }}</code></dd></div>
      </dl>
      <p class="ex-links"><a :href="withBase(run.lean.file)" download="Proof.lean">Download Proof.lean</a></p>
    </div>
  </div>
</template>

<style scoped>
/* Colour key: amber = accepted by the model verifier, green = checked by Lean's kernel, blue = search bookkeeping. */
.ex {
  --ex-model: #8a4b0f;
  --ex-model-bg: #fdf1e1;
  --ex-model-line: #e3a654;
  --ex-model-border: #f0cf9f;
  --ex-kernel: #0d6b3a;
  --ex-kernel-bg: #e5f6ec;
  --ex-kernel-border: #9fd8b6;
  --ex-search: #1d4ed8;
  --ex-search-bg: #e8effe;
  --ex-search-border: #bcd0fb;
}

.dark .ex {
  --ex-model: #f6c98d;
  --ex-model-bg: #3a2a16;
  --ex-model-line: #b7792c;
  --ex-model-border: #6b4a22;
  --ex-kernel: #8fe0b1;
  --ex-kernel-bg: #143325;
  --ex-kernel-border: #24603f;
  --ex-search: #a7c8ff;
  --ex-search-bg: #1b2842;
  --ex-search-border: #34507e;
}

.ex-card {
  border: 1px solid var(--vp-c-divider);
  border-radius: 12px;
  background: var(--vp-c-bg-soft);
  padding: 18px 20px;
}

.ex-eyebrow,
.ex-row-label {
  margin: 0 !important;
  color: var(--vp-c-text-3);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .08em;
  line-height: 1.4 !important;
  text-transform: uppercase;
}

.ex-prompt {
  margin: 8px 0 16px !important;
  color: var(--vp-c-text-1);
  font-family: 'Newsreader Variable', Georgia, serif;
  font-size: 26px;
  line-height: 1.3 !important;
}

.ex-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 20px;
  margin: 0;
  padding-top: 14px;
  border-top: 1px solid var(--vp-c-divider);
}

.ex-meta div { min-width: 0; }
.ex-meta .ex-wide { grid-column: 1 / -1; }
.ex-meta dt { color: var(--vp-c-text-3); font-size: 12px; font-weight: 600; }
.ex-meta dd { margin: 2px 0 0; color: var(--vp-c-text-1); font-size: 14px; line-height: 1.5; overflow-wrap: break-word; }
.ex-meta code { font-size: 12.5px; white-space: normal; }

.ex-chip {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 650;
  line-height: 1.6;
  white-space: nowrap;
}

.ex-chip-model { color: var(--ex-model); background: var(--ex-model-bg); border-color: var(--ex-model-border); }
.ex-chip-kernel { color: var(--ex-kernel); background: var(--ex-kernel-bg); border-color: var(--ex-kernel-border); }

.ex-basis {
  margin: 0 0 14px !important;
  color: var(--vp-c-text-2);
  font-size: 14px;
  line-height: 1.7 !important;
}

.ex-basis .ex-chip,
.ex-lean-status .ex-chip {
  margin-right: 10px;
  vertical-align: 1px;
}

.ex-lemma + .ex-lemma { margin-top: 10px; }
.ex-lemma header { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin-bottom: 8px; }
.ex-lemma header strong { font-size: 15px; color: var(--vp-c-text-1); }
.ex-uses { color: var(--vp-c-text-3); font-size: 13px; }
.ex-lemma header .ex-chip { margin-left: auto; }
.ex-lemma-target { border-color: var(--ex-model-border); }

.ex-math {
  color: var(--vp-c-text-1);
  font-size: 15px;
  line-height: 1.75;
  white-space: pre-line;
}

.ex-math :deep(.katex),
.ex-verifier :deep(.katex) { font-size: 1.05em; }

.ex-lemma details { margin-top: 8px; }
.ex-lemma summary { margin: 0 !important; color: var(--vp-c-brand-1); cursor: pointer; font-size: 13px; font-weight: 600; }
.ex-proof-text { margin-top: 8px; padding-left: 12px; border-left: 2px solid var(--vp-c-divider); color: var(--vp-c-text-2); font-size: 14.5px; }

/* Facts graph: rows at 0–24%, 38–62%, 76–100% of the height. */
.ex-dag {
  --node-h: 92px;
  position: relative;
  height: calc(var(--node-h) / .24);
  margin: 4px 0 14px;
}

.ex-dag-edges {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.ex-dag-edges path {
  fill: none;
  stroke: var(--vp-c-text-3);
  stroke-width: 1.5;
  vector-effect: non-scaling-stroke;
}

.ex-arrow {
  position: absolute;
  width: 0;
  height: 0;
  border-top: 7px solid var(--vp-c-text-3);
  border-right: 5px solid transparent;
  border-left: 5px solid transparent;
  transform: translate(-50%, -100%);
}

.ex-node {
  position: absolute;
  display: flex;
  height: 24%;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--vp-c-divider);
  border-left: 3px solid var(--ex-model-line);
  border-radius: 10px;
  color: var(--vp-c-text-1);
  background: var(--vp-c-bg);
  text-align: left;
  cursor: pointer;
  transition: border-color 150ms ease, box-shadow 150ms ease;
}

.ex-node-0 { top: 0; left: 0; width: 46%; }
.ex-node-1 { top: 0; right: 0; width: 46%; }
.ex-node-2 { top: 38%; left: 25%; width: 50%; }
.ex-node-3 { top: 76%; left: 18%; width: 64%; }

.ex-node-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 11px; line-height: 1.4; }
.ex-node-head b { color: var(--vp-c-text-2); letter-spacing: .04em; text-transform: uppercase; }
.ex-tag { padding: 0 7px; border-radius: 999px; color: var(--ex-model); background: var(--ex-model-bg); font-weight: 650; white-space: nowrap; }
.ex-node-title { font-size: 14px; font-weight: 600; line-height: 1.4; }
.ex-node-title :deep(.katex) { white-space: nowrap; }
.ex-node-target { border-left-width: 5px; }
.ex-node:hover { border-color: var(--vp-c-brand-1); border-left-color: var(--ex-model-line); }
.ex-node[aria-pressed='true'] { border-color: var(--vp-c-brand-1); border-left-color: var(--ex-model-line); box-shadow: 0 0 0 3px var(--vp-c-brand-soft); }
.ex-node:focus-visible { outline: 3px solid var(--vp-c-brand-1); outline-offset: 2px; }

.ex-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.ex-detail-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; margin: 0 0 8px !important; color: var(--vp-c-text-3); font-size: 13px; }
.ex-detail-head strong { color: var(--vp-c-text-1); font-size: 15px; }
.ex-verifier,
.ex-lean-line { margin: 12px 0 0 !important; color: var(--vp-c-text-2); font-size: 13.5px; line-height: 1.65 !important; }
.ex-verifier .ex-chip,
.ex-lean-line .ex-chip { margin-right: 4px; vertical-align: 1px; }
.ex-lean-line code { font-size: 12.5px; }

/* Explore glimpse */
.ex-root {
  margin: 0 0 10px !important;
  color: var(--vp-c-text-1);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.6 !important;
}

.ex-lanes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.ex-lane { display: flex; flex-direction: column; }
.ex-lane header { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.ex-lane header strong { color: var(--vp-c-text-1); font-size: 14.5px; line-height: 1.4; overflow-wrap: anywhere; }
.ex-lane-dead { border-style: dashed; background: transparent; }
.ex-approach { margin: 6px 0 12px !important; color: var(--vp-c-text-2); font-size: 13.5px; line-height: 1.6 !important; }
.ex-row-label { margin-top: 10px !important; }

.ex-status {
  display: inline-block;
  padding: 0 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .04em;
  line-height: 1.8;
  text-transform: uppercase;
  white-space: nowrap;
}

.ex-root .ex-status { margin-right: 10px; vertical-align: 1px; }
.ex-status-closed { color: var(--ex-search); background: var(--ex-search-bg); }
.ex-status-open,
.ex-status-dead { color: var(--vp-c-text-2); background: var(--vp-c-bg-mute); }

.ex-goals,
.ex-verdicts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 6px 0 0 !important;
  padding: 0 !important;
  list-style: none;
}

.ex-goals li,
.ex-verdicts li {
  display: inline-flex;
  gap: 6px;
  margin: 0 !important;
  padding: 2px 9px;
  border: 1px solid var(--vp-c-divider);
  border-radius: 7px;
  color: var(--vp-c-text-2);
  background: var(--vp-c-bg);
  font-size: 12.5px;
  line-height: 1.6;
}

.ex-goals li b { color: var(--vp-c-text-1); }
.ex-goal-closed { border-color: var(--ex-search-border) !important; }
.ex-goal-closed span { color: var(--ex-search); }
.ex-goal-open span { color: var(--vp-c-text-2); }
.ex-goal-dead span { color: var(--vp-c-text-2); text-decoration: line-through; }
.ex-verdicts li { border-color: var(--ex-model-border); }
.ex-verdicts li b { color: var(--ex-model); }
.ex-attempt { margin: 6px 0 0 !important; color: var(--vp-c-text-2); font-size: 13.5px; }
.ex-attempt b { color: var(--vp-c-text-1); font-weight: 600; }
.ex-promoted { margin: 10px 0 0 !important; color: var(--vp-c-text-1); font-size: 13.5px; font-weight: 600; }
.ex-muted { color: var(--vp-c-text-2); font-weight: 500; }

.ex-why {
  margin: auto 0 0 !important;
  padding-top: 12px;
  color: var(--vp-c-text-2);
  font-size: 13px;
  line-height: 1.6 !important;
}

.ex-why::before {
  display: block;
  margin-top: 4px;
  margin-bottom: 10px;
  border-top: 1px solid var(--vp-c-divider);
  content: '';
}

.ex-counts {
  margin: 12px 0 0 !important;
  color: var(--vp-c-text-2);
  font-size: 13px;
  line-height: 1.6 !important;
}

/* Lean */
.ex-lean-status { margin: 0 0 14px !important; color: var(--vp-c-text-1); font-size: 15px; }
.ex-lean-meta { border-top: 0; padding-top: 0; }
.ex-sha { font-size: 11.5px !important; word-break: break-all; }
.ex-links { display: flex; flex-wrap: wrap; gap: 8px 22px; margin: 16px 0 0 !important; font-size: 14px; }
.ex-links a { font-weight: 600; text-decoration: none; }
.ex-links a:hover { text-decoration: underline; }

@media (max-width: 639px) {
  .ex-card { padding: 14px 14px; }
  .ex-prompt { font-size: 22px; }
  .ex-meta { grid-template-columns: 1fr; }
  .ex-dag { --node-h: 108px; }
  .ex-node { gap: 4px; padding: 8px 9px; }
  .ex-node-title { font-size: 12.5px; line-height: 1.35; }
  .ex-node-head { font-size: 10px; }
  .ex-tag { padding: 0 5px; }
  .ex-node-3 { left: 12%; width: 76%; }
  .ex-lanes { grid-template-columns: 1fr; }
  .ex-lemma header .ex-chip { margin-left: 0; }
}

@media (max-width: 359px) {
  .ex-dag { --node-h: 120px; }
  .ex-node-head { flex-direction: column; align-items: flex-start; gap: 2px; }
}

@media (prefers-reduced-motion: reduce) {
  .ex-node { transition: none; }
}
</style>
