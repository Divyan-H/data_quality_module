"""Fix-with-agent: the analyst types what to change; the agent turns it into a typed change-set, shows a preview, and
applies it only after a click, through the same audit and re-check path as an uploaded review workbook.

Layers (each depends only on the ones above it):
    operations   the contract: which changes exist and their parameters (pydantic models)
    selectors    which rows a change touches (conditions, Excel rows, tables)
    session      one editing pass over loaded tables: records every effect, used for both preview and apply
    executors    one function per operation, writing through the session
    describe     plain-language text for operations and previews (code, never the model)
    workspace    a phase's data: load, dry-run, apply, save, snapshot / restore (Phase B: HygieneWorkspace)
    planner      instruction -> plan: a deterministic command parser first, the local LLM for the rest
    store        change-sets and the conversation, as files in the run folder
    service      the use cases the worker and API call: message, apply, undo, discard
    views        read-only data for the UI: grouped issues, pages of rows with highlights

See docs/agent_architecture.md.
"""
