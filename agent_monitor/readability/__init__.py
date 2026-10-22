"""Ansatz Readability: rewrite a manuscript so that a reader can follow it.

A readability run (``run_kind: "readability"``, engine ``readability``) reads
a manuscript the way Analyze does (``agent_monitor.analyze.sources`` and the
sandboxed segmenters) and rewrites it part by part for an audience, keeping
every statement and argument.  An independent model call checks each
rewritten part against the original.  Every judgement is a model's, never a
certificate.

Modules:

* :mod:`.limits` — caps per role and route, and the switches;
* :mod:`.request` — ``readability/request.json`` written by ``jobs.start_job``;
* :mod:`.prompts` — the four system prompts, their pack hash and the prompt
  builders;
* :mod:`.parse` — strict, fail-closed parsing of the model replies;
* :mod:`.structure` — the results of a Markdown or text manuscript (a CGM
  fact closure, result headings);
* :mod:`.document` — the manuscript in reading order, split into parts;
* :mod:`.paper_map` — the paper map (results and dependencies) and notation;
* :mod:`.references` — the printed names of labels, and LaTeX references
  written out;
* :mod:`.report` — ``readable.md``;
* :mod:`.ask` — questions about a finished run;
* :mod:`.record` — the ``run["readability"]`` block of a run record.

The runner is ``agent_monitor/runners/readability_runner.py``; the console
side is ``agent_monitor/readability_runs.py``.
"""
