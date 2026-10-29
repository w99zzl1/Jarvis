from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import threading


if os.name == 'nt':
    kernel32 = ctypes.windll.kernel32

    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_longlong),
            ('PerJobUserTimeLimit', ctypes.c_longlong),
            ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', wintypes.DWORD),
            ('SchedulingClass', wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ('ReadOperationCount', ctypes.c_ulonglong),
            ('WriteOperationCount', ctypes.c_ulonglong),
            ('OtherOperationCount', ctypes.c_ulonglong),
            ('ReadTransferCount', ctypes.c_ulonglong),
            ('WriteTransferCount', ctypes.c_ulonglong),
            ('OtherTransferCount', ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ('IoInfo', IO_COUNTERS),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
else:
    kernel32 = None

_lock = threading.RLock()
_jobs = {}
_local = threading.local()


def set_current_cid(cid):
    _local.cid = cid


def get_current_cid():
    return getattr(_local, 'cid', None)


def _create_job():
    if os.name != 'nt' or not kernel32:
        return None
    h = kernel32.CreateJobObjectW(None, None)
    if not h:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    if not kernel32.SetInformationJobObject(h, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(h)
        return None
    return h


def ensure_job(conversation_id):
    if not conversation_id or os.name != 'nt':
        return None
    with _lock:
        job = _jobs.get(conversation_id)
        if job:
            return job
        h = _create_job()
        if not h:
            return None
        _jobs[conversation_id] = {'handle': h, 'procs': set()}
        return h


def assign_proc(conversation_id, proc):
    if not conversation_id or os.name != 'nt' or proc is None:
        return
    h = ensure_job(conversation_id)
    if not h:
        return
    try:
        if proc.poll() is not None:
            return
        if getattr(proc, '_handle', None):
            kernel32.AssignProcessToJobObject(h, int(proc._handle))
    except Exception:
        pass
    try:
        with _lock:
            st = _jobs.get(conversation_id)
            if st and getattr(proc, 'pid', None):
                st['procs'].add(int(proc.pid))
    except Exception:
        pass


def stop_job(conversation_id):
    if not conversation_id or os.name != 'nt':
        return {'stopped': False, 'killed': 0}
    with _lock:
        st = _jobs.pop(conversation_id, None)
    if not st:
        return {'stopped': False, 'killed': 0}
    h = st.get('handle')
    try:
        if h:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return {'stopped': True, 'killed': len(st.get('procs') or [])}


def cleanup_job(conversation_id):
    if not conversation_id:
        return
    with _lock:
        st = _jobs.pop(conversation_id, None)
    if not st:
        return
    h = st.get('handle')
    try:
        if h:
            kernel32.CloseHandle(h)
    except Exception:
        pass
