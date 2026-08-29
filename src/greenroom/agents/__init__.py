"""ADK agents. Each one is constructed with only the tools it is allowed to use.

Tool scoping is structural, not instructional: the read-side agents are never handed
a send tool, so no amount of prompt injection can make them send.
"""
