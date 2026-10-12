---
title: "Overview has moved"
description: "The platform overview is now part of the Ansätze Quickstart."
sidebar: false
aside: false
search: false
head:
  - - link
    - rel: canonical
      href: https://ansatze.ai/quickstart
  - - meta
    - name: robots
      content: noindex,follow
---

<script setup>
import { onMounted } from 'vue'
import { withBase } from 'vitepress'
onMounted(() => {
  const aliases = { '#proving-engines': '#proving-engines', '#real-time-trace-views': '#views-in-a-run', '#persistent-library-memory-skills-tools': '#collaborate-and-share', '#persistent-library-memory-·-skills-·-tools': '#collaborate-and-share', '#project-layout': '#good-to-know', '#account-boundaries': '#guests-and-accounts', '#where-to-go-next': '#good-to-know', '#cgm-pipeline': '#how-a-cgm-run-works' }
  let hash = window.location.hash
  try { hash = decodeURIComponent(hash) } catch {}
  window.location.replace(withBase('/quickstart') + window.location.search + (aliases[hash] || window.location.hash))
})
</script>

# The overview is now in Quickstart

Read the **[Quickstart](/quickstart)** for the first-run guide, a [worked example](/quickstart#example), [how an Ansatz CGM run works](/quickstart#how-a-cgm-run-works) (strategist → workers → independent verifier → curator, then Lean) and the proving engines.
