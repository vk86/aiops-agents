"""AIOps transaction agent: follows a business transaction through the CMDB dependency chain, shows the health of
each hop (open incidents, recent changes) and where the failure most likely starts. Per-request traces (Tempo,
Jaeger, Dynatrace) plug in later; transaction ids are searched in incidents and work notes today."""
