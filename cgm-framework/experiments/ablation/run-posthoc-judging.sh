#!/usr/bin/env bash
# Post-hoc strict judging of the ab3 (self-verify gate) fact graphs by the REAL
# verifier. Run ONLY when no swarm is active — ablation-usage.py attributes verify
# runs by time window, so judging during a run pollutes that run's verifier column.
#
#   bash experiments/ablation/run-posthoc-judging.sh
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/env.sh
LOG="$CGM_ROOT/runtime/logs/posthoc-judging.log"
say(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

J="$CGM_PY experiments/ablation/judge-facts.py"

say "=== ab3_fpb06_treelattice: all 15 facts ==="
$J --project ab3_fpb06_treelattice --all \
   --facts 89d178de2db3534b,e2639a6107608a43,d14e2dc6996c2372,ebbf9d0db63a7ca3 2>&1 | tee -a "$LOG"

say "=== ab3_fpb03_bernoulli: 10 targets + 12 sampled of 103 ==="
$J --project ab3_fpb03_bernoulli --sample 12 \
   --facts db85da30d20e8062,8c2f0fe26a597780,6da00b267777eeb7,f95a7df39a83df1a,9ef409a53a8a575f,3b1b732dd8ea23f3,8ba44b634098baa6,93f46f080143de17,a5f8683b8fcacb49,ef4ed199f23ec3e3 2>&1 | tee -a "$LOG"

say "=== ab3_fpb10_vonneumann: 9 targets + 12 sampled of 184 ==="
$J --project ab3_fpb10_vonneumann --sample 12 \
   --facts eaba61466c3a4fab,78bc3dcddce9d398,5fdfa836c4d8f408,8ad64d32ee9ce106,d53deeff7bcc16d2,bfdc4836a35dd01a,bd3a2a6c20c7dd13,337678f5777a0098,741bbb90e5079460 2>&1 | tee -a "$LOG"

say "=== post-hoc judging complete ==="
