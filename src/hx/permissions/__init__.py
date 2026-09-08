"""Two independent safety layers: a rule engine and an OS sandbox.

Both must pass. The rule engine decides intent (may this be attempted?); the
sandbox enforces reality (what can the process actually touch?). Neither is
trusted to be sufficient alone - a mis-parsed command escapes the engine, and a
sandbox cannot tell a wanted `rm` from an unwanted one.
"""
