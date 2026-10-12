# CGM evaluation scripts and inputs

This directory retains CGM experiment sequencers, judging tools, kickoff prompts,
and historical runbooks. Generated `results/` directories are excluded from Git.
Runbooks may refer to the original local output layout; those references describe
where a run writes its artifacts, not evidence bundled with this repository.

The experiment families are `ablation/`, `memrun/`, and `erdos/`. Read the chosen
script and runbook and configure the framework before starting a run: these are
live model experiments, not part of the offline test suite.

Historical notes describe exposure to prior answers in some ablation setups.
The retained prompts and runbooks are research inputs, not a guarantee of a
blinded protocol or reproduced benchmark result. Keep operator profiles and
external memory controlled when designing a new evaluation.
