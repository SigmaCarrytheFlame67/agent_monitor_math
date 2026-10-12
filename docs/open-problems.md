---
title: "Open Problems is now Problems"
description: "The Open Problems page moved to Problems."
sidebar: false
aside: false
search: false
head:
  - - link
    - rel: canonical
      href: https://ansatze.ai/problems
  - - meta
    - name: robots
      content: noindex,follow
---

<script setup>
import { onMounted } from 'vue'
import { withBase } from 'vitepress'
// Keep ?source=, ?problem= and any #hash from old links.
onMounted(() => window.location.replace(withBase('/problems') + window.location.search + window.location.hash))
</script>

# Open Problems is now Problems

Continue to **[Problems & Forum](/problems)**: solved, AI-proposed, partial and open problems, each with its sources.
