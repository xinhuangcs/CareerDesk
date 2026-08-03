---
name: prepare-for-interview
description: Build a focused plan for an upcoming interview from the target role, research, résumé, knowledge gaps, and practice history; use when the user asks how to prepare for a specific interview.
tools: [query_timeline, query_prep, request_application_prep, query_library, query_study, query_grill, query_status]
---
# Prepare for an interview

1. Use `query_timeline` to identify the company, role, current step, and timing. Do not ask again for facts already supplied by the user.
2. Use `query_prep` to read the company and role research. State any gaps plainly and never invent company developments. Use `request_application_prep` only when the user explicitly asks to generate or refresh the research; after starting it, provide the page entry and do not wait or poll in the same turn.
3. Use `query_library` for the relevant résumé and `query_study` for role-specific weaknesses and questions.
4. If practice history exists, use `query_grill` for observed performance. Use `query_status` only when recurring state factors would materially help preparation.
5. Rank a small set of actions by “most likely to be asked and currently weakest,” and identify the local data source supporting each priority.

Start with two or three lines identifying the interview, then give a prioritized list that says what to prepare, why it matters, and which data supports it. Keep the result readable in one screen instead of reproducing all source data.

This workflow is read-only by default. Do not call a write tool or claim a saved artifact unless the user separately and explicitly asks for a change.
