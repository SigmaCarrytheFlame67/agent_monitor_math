<script setup>
// Shape plus colour for each problem category, so the status never depends on colour alone.
defineProps({ category: { type: String, required: true } })
</script>

<template>
  <svg class="problem-category-mark" :data-category="category" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
    <path v-if="category === 'solved'" d="M3.2 8.6 6.4 11.6 12.8 4.6" />
    <path v-else-if="category === 'ai-proposed'" d="M8 2.2 13.8 8 8 13.8 2.2 8Z" />
    <template v-else-if="category === 'partial'"><circle cx="8" cy="8" r="5.6" /><path class="fill" d="M8 2.4a5.6 5.6 0 0 1 0 11.2Z" /></template>
    <circle v-else cx="8" cy="8" r="5.6" />
  </svg>
</template>

<style scoped>
.problem-category-mark { flex: none; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.problem-category-mark[data-category='solved'] { stroke-width: 2.1; }
.problem-category-mark .fill { fill: currentColor; stroke: none; }
</style>
