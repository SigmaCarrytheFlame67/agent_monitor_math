<script setup>
import { computed } from 'vue'
import { renderMath } from './mathText.js'
import './vendor/katex/katex.min.css'
// `inline` renders one-line text (a title, a headline, an evidence note) as a span: its $…$ math is typeset like the statement's.
const props = defineProps({ text: { type: String, default: '' }, inline: { type: Boolean, default: false } })
const rendered = computed(() => renderMath(props.text, { speech: props.inline, money: props.inline }))
</script>

<template><span v-if="inline" class="problem-math-inline" v-html="rendered"></span><div v-else class="problem-math" v-html="rendered"></div></template>

<style scoped>
.problem-math { line-height: 1.85; white-space: pre-line; overflow-wrap: anywhere; }
.problem-math :deep(.katex) { white-space: normal; font-size: 1.08em; }
.problem-math :deep(.katex-display) { overflow-x: auto; overflow-y: hidden; padding: 6px 0; max-width: 100%; }
.problem-math-inline :deep(.katex) { white-space: normal; font-size: 1.04em; }
/* Inline formulas sit in links and headings, whose names skip MathML: screen readers get the formula's words instead. */
.problem-math-inline :deep(.math-speech) { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
