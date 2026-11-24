import json
import subprocess
import threading
import queue

from .registry import Tool, ToolResult

monitor_alerts = queue.Queue()
bg_procs = []


def _start_background_ps(script):
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    proc.stdin.write(script + "\n")
    proc.stdin.flush()
    bg_procs.append(proc)

    def _reader():
        while proc.poll() is None:
            line = proc.stdout.readline()
            if line and line.strip():
                monitor_alerts.put(line.strip())

    threading.Thread(target=_reader, daemon=True).start()
    return ToolResult(
        status="success",
        data={"status": "monitoring_started"},
    )


class MonitorTool(Tool):
    name = "monitor"
    description = "Monitor conditions: start (interval 3s) or stop."
    category = "system"
    cost = "low"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop"]},
            "condition": {"type": "string"},
        },
        "required": ["action"],
    }

    def execute(self, args: dict) -> ToolResult:
        action = args.get("action", "")

        if action == "stop":
            global bg_procs
            for proc in bg_procs:
                try:
                    proc.stdin.write("exit\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.terminate()
                except Exception:
                    pass
            bg_procs.clear()
            while not monitor_alerts.empty():
                try:
                    monitor_alerts.get_nowait()
                except Exception:
                    break
            return ToolResult(
                status="success",
                data={"status": "monitoring_stopped"},
            )

        if action == "start":
            condition = args.get("condition", "")
            if not condition:
                return ToolResult(status="error", error="start requires a condition")
            script = f"""
$wasConnected = $false
$deviceName = ""
while($true) {{
    $found = {condition}
    if ($found -and -not $wasConnected) {{
        try {{
            $deviceName = ($found | Select-Object -First 1 -ExpandProperty FriendlyName -ErrorAction Stop)
        }} catch {{
            $deviceName = "device"
        }}
        Write-Output "connected:$deviceName"
        $wasConnected = $true
    }} elseif (-not $found -and $wasConnected) {{
        Write-Output "disconnected:$deviceName"
        $wasConnected = $false
        $deviceName = ""
    }}
    Start-Sleep -Seconds 3
}}
"""
            return _start_background_ps(script)

        return ToolResult(status="error", error=f"Unknown action: {action}")
