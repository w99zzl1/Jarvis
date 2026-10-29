"""
Progress parser for extracting structured progress from command output.
Each parser function takes a line of output and returns progress info if found.
"""
import re
from typing import Optional


class ProgressInfo:
    def __init__(self, progress: int = None, eta: str = None, speed: str = None,
                 bytes_total: int = None, bytes_done: int = None, message: str = None):
        self.progress = progress        # 0-100
        self.eta = eta                  # e.g., "2m13s" or "4 мин"
        self.speed = speed              # e.g., "3.2 MB/s"
        self.bytes_total = bytes_total  # bytes
        self.bytes_done = bytes_done    # bytes
        self.message = message          # raw matched line

    def is_meaningful(self) -> bool:
        return self.progress is not None or self.bytes_done is not None

    def __repr__(self):
        parts = []
        if self.progress is not None:
            parts.append(f"{self.progress}%")
        if self.speed:
            parts.append(self.speed)
        if self.eta:
            parts.append(f"ETA: {self.eta}")
        return f"<ProgressInfo {' '.join(parts)}>"


def parse_bits_transfer(line: str) -> Optional[ProgressInfo]:
    """
    Parse BITS transfer progress.
    Examples:
        "Transfer started: 1024 bytes"
        "24% complete (1.2 GB / 5.1 GB)"
        "Download complete"
    """
    line = line.strip()
    
    # Completion
    if 'download complete' in line.lower() or 'transfer complete' in line.lower():
        return ProgressInfo(progress=100, message=line)
    
    # Percentage with bytes
    m = re.search(r'(\d+)%\s*complete\s*\(([.\d]+)\s*([KMGT]?B)\s*/\s*([.\d]+)\s*([KMGT]?B)\)', line, re.IGNORECASE)
    if m:
        pct = int(m.group(1))
        done = parse_size(m.group(2), m.group(3))
        total = parse_size(m.group(4), m.group(5))
        return ProgressInfo(progress=pct, bytes_done=done, bytes_total=total, message=line)
    
    # Just percentage
    m = re.search(r'(\d{1,3})%', line)
    if m and len(line) < 100:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    return None


def parse_winget(line: str) -> Optional[ProgressInfo]:
    """
    Parse winget install/update progress.
    Examples:
        "Installing dependencies [47%..."
        "Found existing package. Starting installer... [================ 100% ===]"
    """
    line = line.strip()
    
    m = re.search(r'\[={3,}\s*(\d+)%\s*={3,}\]', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    m = re.search(r'(\d+)%\s*\.\.\.', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    m = re.search(r'\({0,1}(\d+)%\){0,1}\s*(?:complete|done|finished)', line, re.IGNORECASE)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    return None


def parse_sfc(line: str) -> Optional[ProgressInfo]:
    """
    Parse sfc /scannow progress.
    Examples:
        "Beginning system scan..."
        "Verification 73% complete"
        "Verification 100% complete"
        "Windows Resource Protection did not find any integrity violations"
    """
    line = line.strip()
    
    if 'did not find any integrity violations' in line.lower():
        return ProgressInfo(progress=100, message=line)
    
    if 'cannot repair' in line.lower():
        return ProgressInfo(progress=100, message=line)
    
    m = re.search(r'[Vv]erification\s+(\d+)%\s*complete', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    m = re.search(r'[Ss]can\s+(\d+)%', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    return None


def parse_curl(line: str) -> Optional[ProgressInfo]:
    """
    Parse curl download progress.
    Examples:
        "  0  1024M    0  9216k    0 --Time--    0     0 --:--:--  0:00:01 --:-- --  9216k"
        "100 1024M    0  1024M    0  0:01:23  0:00:45  0:00:38 1024M"
    """
    line = line.strip()
    
    # ASCII progress bar: "  0  1024M    0  9216k"
    m = re.search(r'^[\s%#]+(\d+)%\s+([.\d]+)([KMG])', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    # wget/curl style: "100 1024M ..."
    if line.startswith(('100 ', '  0', ' 10', ' 20', ' 30', ' 40', ' 50', ' 60', ' 70', ' 80', ' 90')):
        m = re.search(r'^[\s]*(\d+)\s+([.\d]+)([KMG])', line)
        if m and int(m.group(1)) <= 100:
            return ProgressInfo(progress=int(m.group(1)), message=line)
    
    return None


def parse_dism(line: str) -> Optional[ProgressInfo]:
    """
    Parse DISM progress.
    Examples:
        "[==========================100==========================]"
        "The operation completed successfully."
    """
    line = line.strip()
    
    if 'operation completed successfully' in line.lower():
        return ProgressInfo(progress=100, message=line)
    
    m = re.search(r'\[={2,}\s*(\d+)%\s*={2,}\]', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    # DISM-style: [=======100========]
    m = re.search(r'\[={2,}(\d+)={2,}\]', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    return None


def parse_chocolatey(line: str) -> Optional[ProgressInfo]:
    """
    Parse Chocolatey progress.
    Examples:
        "Progress: 48%"
    """
    line = line.strip()
    
    m = re.search(r'[Pp]rogress:\s*(\d+)%', line)
    if m:
        return ProgressInfo(progress=int(m.group(1)), message=line)
    
    return None


def parse_generic(line: str) -> Optional[ProgressInfo]:
    """
    Generic progress detection - last resort.
    Looks for percentage anywhere in the line, with some context.
    """
    line = line.strip()
    
    if not line or len(line) > 200:
        return None
    
    # Skip non-progress lines
    skip_words = ['error', 'warning', 'info:', 'usage:', 'help', 'command', 'parameter', 'invalid']
    if any(w in line.lower() for w in skip_words):
        return None
    
    # Simple percentage
    m = re.search(r'^[\s\[\(]*(\d{1,3})%\s*[\]\)\s\.\w]*$', line)
    if m:
        pct = int(m.group(1))
        if 0 <= pct <= 100:
            return ProgressInfo(progress=pct, message=line)
    
    return None


def parse_size(value: str, unit: str) -> int:
    """Convert size string to bytes."""
    multipliers = {
        'B': 1,
        'KB': 1024,
        'MB': 1024**2,
        'GB': 1024**3,
        'TB': 1024**4,
    }
    try:
        val = float(value)
        return int(val * multipliers.get(unit.upper(), 1))
    except (ValueError, TypeError):
        return 0


# Parser registry - order matters (specific parsers first)
PARSERS = [
    parse_bits_transfer,
    parse_winget,
    parse_sfc,
    parse_curl,
    parse_dism,
    parse_chocolatey,
    parse_generic,
]


def parse_line(line: str) -> Optional[ProgressInfo]:
    """Try each parser in order, return first meaningful result."""
    for parser in PARSERS:
        result = parser(line)
        if result and result.is_meaningful():
            return result
    return None


def parse_bytes_speed(line: str) -> Optional[ProgressInfo]:
    """
    Extract download speed from a line containing byte counts.
    Example: "1247 MB / 5.1 GB  Speed: 3.2 MB/s"
    """
    # Try to find speed
    m = re.search(r'[:\s]?\s*([.\d]+)\s*([KMG]?B)/s', line, re.IGNORECASE)
    if m:
        speed = f"{m.group(1)} {m.group(2).upper()}/s"
    
    # Try to find ETA
    eta_m = re.search(r'(?:ETA|eta)[:\s]+(.+?)(?:\s*$|\s+\d+%)', line)
    if eta_m:
        eta = eta_m.group(1).strip()
    
    return None