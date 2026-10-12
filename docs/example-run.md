---
title: "Example run has moved"
description: "The worked example is now part of the Ansätze Quickstart."
sidebar: false
aside: false
search: false
head:
  - - link
    - rel: canonical
      href: https://ansatze.ai/quickstart#example
  - - meta
    - name: robots
      content: noindex,follow
---

<script setup>
import { onMounted } from 'vue'
import { withBase } from 'vitepress'
onMounted(() => {
  const moved = { '#work-on-a-problem-together': '#collaborate-and-share', '#share-a-result-without-editing-access': '#collaborate-and-share' }
  window.location.replace(withBase('/quickstart') + window.location.search + (moved[window.location.hash] || '#example'))
})
</script>

# The example run is now in Quickstart

Continue to the **[worked example in Quickstart](/quickstart#example)**: a real guest run with its proof, Facts and Explore graphs, and the Lean 4 file the kernel accepted.
