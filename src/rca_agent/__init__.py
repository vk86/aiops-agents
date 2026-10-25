"""AIOps RCA agent: a separate root cause analysis capability.

Evidence sources (ServiceNow today; Git, releases and MELT pluggable) -> RCA engine (ranked hypotheses,
timeline, metrics) -> RCA template report -> publish to ServiceNow with approval.
See docs/RCA_TEMPLATE.md.
"""
