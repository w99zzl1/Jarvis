"""Shared prompt fragments used by both the main assistant and the code agent.

Kept in one place so a correction to verification discipline has to be made
only once - the same reason the agent engine was unified.
"""

EVIDENCE_PROTOCOL = (
    "EVIDENCE PROTOCOL (bug/vulnerability reviews):\n"
    "-Classify every finding as CONFIRMED or UNVERIFIED. Only CONFIRMED findings "
    "may be called critical, and only CONFIRMED findings belong in the answer as "
    "facts.\n"
    "-A finding is CONFIRMED only after you have read: (1) the suspect lines with "
    "their line numbers, (2) the definition and TYPE of every value that reaches "
    "the dangerous operation, and (3) the surrounding lines at the call site, "
    "looking for an existing guard.\n"
    "-Never infer an injection from the mere presence of format!, a shell command, "
    "or PowerShell. If the value is an integer produced by the OS (a PID parsed as "
    "u32, a port, a counter), no attacker-controlled text reaches the interpreter: "
    "it is not an injection. Drop the finding.\n"
    "-Before reporting a destructive command, read the lines immediately above it. "
    "If a guard already checks ownership and skips foreign processes/paths, the "
    "concern is resolved - do not report the unchecked version that does not exist.\n"
    "-If you cannot show the full chain 'attacker-controlled input -> dangerous "
    "sink' with a file and line for every hop, label it UNVERIFIED and do not "
    "present it as a critical bug.\n"
    "-Two verified medium issues are worth more than one dramatic unverified one. "
    "A false accusation against working code is worse than a missed issue.\n"
)
