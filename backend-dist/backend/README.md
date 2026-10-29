# Generated backend — do not edit

This folder is where the packaged Python backend is assembled:

```
backend/
  python/            embeddable CPython 3.10 + Lib\site-packages
  app/               server/, shared/, agents/, config/
  app/jarvis_disk.pyd
```

It is **generated** by:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_backend.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_disk_scan.ps1
```

`src-tauri/tauri.conf.json` maps this folder to `$RESOURCE/backend` in the
installer. Keep this README so the folder exists before the first backend
build; Tauri refuses to compile when a declared resource path is missing.
Everything except this file is git-ignored.
