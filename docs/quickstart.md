---
title: "Quickstart"
description: "Prove your first statement with Ansätze in a few minutes, then walk through a real run: the proof, its Facts and Explore graphs, and the Lean 4 file the kernel accepted."
---

<script setup>
import { onBeforeUnmount, onMounted } from 'vue'
import ExampleRun from './.vitepress/theme/ExampleRun.vue'
import GuideVideo from './.vitepress/theme/GuideVideo.vue'

// Sections of the earlier Quickstart and Example run pages, mapped to where they live now.
const moved = {
  '#1-open-the-console': '#guests-and-accounts', '#2-choose-how-runs-are-billed': '#guests-and-accounts',
  '#3-start-a-problem': '#quickstart', '#4-review-and-continue': '#example',
  '#trace-views-and-proof-graphs': '#views-in-a-run', '#reusable-memory-skills-and-tools': '#collaborate-and-share',
  '#project-layout': '#good-to-know', '#keep-exploring': '#good-to-know'
}
function follow() {
  const target = moved[window.location.hash]
  if (!target) return
  history.replaceState(history.state, '', target)
  document.getElementById(target.slice(1))?.scrollIntoView()
}
onMounted(() => {
  follow()
  window.addEventListener('hashchange', follow)
})
onBeforeUnmount(() => window.removeEventListener('hashchange', follow))
</script>

# Quickstart

Prove a statement in a few minutes, with no account. Watch a real run (34&nbsp;s), then follow the four steps yourself.

<GuideVideo />

<ol class="qs-steps">
<li><strong>Continue as guest</strong><p>Select <a href="/guest">Try as guest</a>. You need no account or API key while hosted access is available. To keep your history in an account, <a href="/sign-in">sign in</a> instead.</p></li>
<li><strong>Type a problem</strong><p>Enter a statement such as <em>Prove that the sum of two odd integers is even.</em> You can also pick one from <a href="/problems">Problems</a>. Then select <b>Prove →</b>. Guests run <b>Ansatz CGM · Kimi K3</b>.</p></li>
<li><strong>Watch the pipeline</strong><p>Open <b>Monitor</b> to follow the CGM stages. A strategist plans, workers prove lemmas, an independent verifier checks each lemma and a curator scores the search.</p></li>
<li><strong>Read the result</strong><p><b>Sandbox</b> shows the proof. <b>DAG → Facts</b> and <b>DAG → Explore</b> show what the proof depends on and how the search went. <b>Lean Verification</b> checks the proof in Lean 4 inside a sandbox.</p></li>
</ol>

::: tip Two kinds of check
**Model-verifier accepted** means another model call judged a lemma’s proof correct. It is useful evidence, but it is **not a Lean certificate**. **Verified** in Lean Verification means Lean’s kernel accepted the file with no `sorry` and only standard axioms, and the statement check found no major mismatch with the problem.
:::

## A real run, start to finish {#example}

This is an unedited guest run on ansatze.ai that followed the four steps above. The proof, the graphs and the Lean file are copied from the run’s saved files, with the session, run identifiers and server paths removed. The short card titles and summaries are ours.

### The problem {#example-problem}

<ExampleRun part="problem" />

### The proof {#example-proof}

The run published three lemmas and the target theorem. Math is typeset for the web; the wording is exactly what the run wrote.

<ExampleRun part="proof" />

### Facts graph {#example-facts}

Facts holds only statements the verifier accepted: what the proof rests on. An arrow points from a lemma to a result that cites it. Select a node to read it and see its Lean declaration.

<ExampleRun part="facts" />

### Explore graph {#example-explore}

Explore records how the search went. The strategist proposed two plans, each an AND group of subgoals meant to be proved together. Plan 1’s worker proved the target, the verifier accepted every step, and those four statements became the facts above. The curator marked Plan 2 dead.

<ExampleRun part="explore" />

### Lean 4 check {#example-lean}

The Lean stage fixed the formal statement first, then proved it, and the sandboxed checker accepted the file. Each fact above is linked to a declaration in it.

<ExampleRun part="lean" />

<div class="qs-lean-code">

<<< @/public/examples/odd-sum/Proof.lean

</div>

## How a CGM run works {#how-a-cgm-run-works}

The Ansatz CGM pipeline is tool-free. Each stage is a single model response, and the console’s own code enforces the budget, the output format and the dependency rules.

1. **Strategist.** Reads the problem and proposes a few plans. Each plan names a technique, such as a direct proof, an invariant or a reduction, and may split the goal into an AND group of subgoals meant to be proved together.
2. **Workers.** Each worker follows one plan and writes numbered lemmas and a final theorem. Each lemma has a statement and a proof that cites only earlier lemmas.
3. **Independent verifier.** Each lemma is checked in its own fresh model call. That call sees only the lemma and the statements it cites. A lemma that fails gets a bounded repair attempt, and only accepted lemmas become **facts**.
4. **Curator.** Scores the exploration graph and marks goals as open, active, blocked, dead or closed. Code, not the model, applies the rules that matter: a subgoal closes only with a verifier-accepted fact, a plan closes when its worker’s proof of the target is accepted (even with subgoals still open), and a plan with a dead subgoal is dead.
5. **Formal stage.** Fixes the Lean statement first, then proves it, and reports compile and axiom checks.

Two more things to know when reading a result:

- **Compiles — wrong statement** in Lean Verification means the file proves something other than the problem. The statement check is itself a model check, so still read the formal statement. A *minor* flag is shown as a caveat and gets one statement-fix attempt; it never changes the status by itself.
- **Explore** records the search. Its edges are not proof steps.

The CGM prompts and data formats are adapted from the Ansatz *continual-graph-memory* project by FrenzyMath, used under the Apache License 2.0. The console’s runner and graph validator are original code.

## Analyze a manuscript {#analyze}

**Analyze** checks an existing paper result by result instead of proving something new. It is the third choice under the problem box, next to *Informal only* and *Informal + formalization*, and appears when the site offers it.

1. **Choose Analyze** and paste one link: an arXiv paper, a GitHub file or folder, a manuscript from [openai/math](https://github.com/openai/math), or a direct PDF or TeX file. You can also paste the manuscript text or attach its files. A link is previewed before anything runs; for the openai/math repository, pick one manuscript from its list.
2. **Optionally** name a result to check first (*Focus*), and tick *Also formalize main statements in Lean* to state the main results in Lean. Only the statements are formalized; proofs are not attempted.
3. **Select Analyze →.** The run goes through five stages: Ingest, Segment, Check, Roll-up and Report.
4. **Read the result.** **Sandbox** shows the report: the main results, a verdict for every checked result with its location, and the external results the proofs rely on. **DAG → Manuscript** draws the manuscript's own results and references, coloured by verdict. Select a result to read its statement, issues and notes, to **Re-check** it, to **Formalize** its statement, or to open it in the source.

::: warning Checked by a model verifier — not a certificate
A verdict is one model call's judgement of a proof as written, given the statements it cites: blue *checked*, amber *gap*, red *error*, grey *unclear*, hollow *not checked* (a reply the verifier's output could not be read from counts as not checked). It can be wrong in both directions, and it is not a refereed review or a Lean proof. Lean statements for an analysis read "Statements compiled · proofs not attempted"; upstream Lean files, such as those in openai/math, are linked and are not re-checked by this server.
:::

Guests can analyze arXiv, GitHub and openai/math links, pasted text of up to 20,000 characters, or text files: one analysis per hour, with up to 12 results checked, and up to 2 re-checks per analysis. Accounts can also analyze PDFs, zip or tar.gz bundles and other file links, on the included Kimi K3 or their own API key. Analyze sends the manuscript text to the selected model provider, so don't submit work you aren't allowed to share. An analysis can't be continued: start a new one, or re-check a result.

On [Problems](/problems), **Analyze the claimed solution** opens a claimed proof's manuscript here with its link filled in. Nothing runs until you select **Analyze →**.

## Good to know {#good-to-know}

### Guests and accounts {#guests-and-accounts}

- A guest session gets **10 run starts** by default, at most six per hour. Deleting a run does not give a start back. Guest runs use a smaller CGM budget, and guest Lean and DAG actions have an hourly limit. The console tells you when you can try again.
- Guest history is tied to a 24-hour cookie in that browser. It is lost if you clear site data or switch browser. It does not move into an account later, and ending the session deletes any saved provider key.
- If hosted Kimi access is unavailable, add your own Kimi API key in **Settings**.
- Anyone can create an account: open [Sign up](/sign-in) and use an email and password. An account keeps your runs private and lets you add your own API keys, add feedback and continue a run, and set the iteration budget. Accounts run **Ansatz CGM** and **Plain**, and can [analyze manuscripts](#analyze). Kimi K3 is included; any other model runs on your own API key, which you add under **Settings → API providers** (an Anthropic key for Claude models, an OpenAI key for GPT models). Accounts have hourly limits on run starts and on feedback messages, and a feedback message holds up to 20,000 characters.
- Signing in with a Claude or ChatGPT subscription, which Claude Code and Codex use, is only for the site administrator. Anthropic does not let third-party sites sign in with or route requests through members’ Claude plans, and OpenAI requires partner approval for hosted sites to use members’ ChatGPT plans. To use Claude or OpenAI models, add your own API key and choose the model in Ansatz CGM or Plain. The coding-agent harnesses (Codex, Claude Code, OpenClaw, OpenHands and others) also run tools on the server, so only the site administrator can use them.
- Provider keys saved in the console are encrypted at rest. Signed-in local proving engines share the host’s operating-system trust boundary; they are not a sandbox for untrusted code.
- Forgot your password? Choose **Forgot password?** on the sign-in page and use only the newest reset email. Each link works once.

### Proving engines {#proving-engines}

Availability depends on the deployment and on the account’s connected providers. Each run has its own workspace.

| Engine | Workflow |
| --- | --- |
| **Ansatz CGM · Kimi K3** | The CGM pipeline on Kimi K3; the default for guests. Publishes Facts and Explore graphs. |
| **Ansatz CGM harness** | The same pipeline on a model from your own API key. The site administrator can also run it on a linked Codex or Claude Code subscription. |
| **IMProof** | An author–critic proving workflow based on ProofStack. |
| **Hermes** | An embedded agent harness for reasoning and tools. |
| **DeepAgents** | A LangGraph agent with a dedicated runtime. |
| **Meta-Harness** | A solver, evaluator and proposer loop. |
| **OpenClaw** | An embedded local agent. |
| **Codex, Claude Code / OpenClaude, OpenHands** | Coding-agent integrations for the site administrator. Claude Code runs on the administrator’s linked subscription; Codex on that subscription or an API key. |
| **Plain** | A single-model-call baseline. |
| **Ansatz Analyze** | The engine of the [Analyze](#analyze) mode: checks an existing manuscript result by result with a model verifier. Tool-free, on the included Kimi K3 or your own API key. |

### Views in a run {#views-in-a-run}

- **Monitor** records agent activity, token usage and cost. **Map** shows a free-form graph of that activity; **Pipeline** groups it into stages.
- **DAG** opens on **Informal · CGM** (Explore and Facts). **Outline** is an informal outline of the final proof. **Formal · Lean** and **Compiler facts** show the structure of the Lean file.
- **Lean Verification** shows the source and the checker results. Guests can run **Verify**, **Compile**, **Check** and **Audit** within the guest limits.
- In an analysis, **Sandbox** shows its report, **DAG** opens on **Manuscript** (results coloured by model-verifier verdicts, with filters), **Outline** is the same graph as an outline, and the Lean tab, named **Lean statements**, formalizes statements only.

A graph shows dependencies. It does not certify an argument on its own.

### Collaborate and share {#collaborate-and-share}

- **Collaborative projects** (signed in): create a project, write the problem and notes together, and invite editors, who sign in and accept. Each member launches runs with their own account settings, and project runs appear in **Project results**. Members can inspect every project run, including its prompts and files, so invite only people who should have that access. Only the member who started a run can continue, stop or change it.
- **Share** a run as a read-only link. The link includes the problem, the proof, agent summaries, the saved graphs and the Lean source. It excludes account settings, chat, attachments and the rest of the workspace. You can update or revoke it at any time; revoking cannot recall copies a reader already saved.
- **Library** (signed in): enabled memory, skills and tools are copied into a run’s workspace and added to its prompt context.

### Keep your account safe {#keep-your-account-safe}

- Do not paste API keys into problems or chat messages.
- Do not share password-reset links.
- Sign out on shared computers, and rotate a provider key at once if it may have leaked.

Next: browse [Problems](/problems), explore the [DAG atlas](/dag) or read the [News](/news).
