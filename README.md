# jugginai-pc-bridge

Token-authenticated PC Model Manager + reverse proxy for the Windows PC that runs
NVIDIA PAIR (LM Studio on `:1234`, llama.cpp on `:8080`). It binds to
`127.0.0.1:8899` only and is published as `https://pc.jugginai.net` through a
cloudflared tunnel that runs on the PC. `ai.jugginai.net` reaches it with the
`PC_BRIDGE_TOKEN` Worker secret.

- `bridge.py`: Python 3 stdlib only, no pip installs.
- `start-bridge.bat`: starts `pythonw bridge.py` and `cloudflared tunnel run --token-file ...` (hidden, no .vbs).
- `install.ps1`: user-context installer. No admin, netsh, or firewall changes.

## Install (one step, on the PC)

Put the token files in a `secrets\` folder next to `install.ps1` (or straight into
`%USERPROFILE%\.jugginai\`): `pc-bridge-token`, `cloudflared-token`, and optionally `hf-token`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

It copies the files to `%USERPROFILE%\.jugginai\bridge`, moves the token files to
`%USERPROFILE%\.jugginai\` with owner-only ACLs, downloads `cloudflared.exe` if it's missing,
adds `jugginai-start-bridge.bat` to the Startup folder, restarts only its own processes
(command line contains `.jugginai`), and prints one status line. Logs are in `%USERPROFILE%\.jugginai\logs\`.

## API (all routes need `Authorization: Bearer <PC_BRIDGE_TOKEN>`)

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | engines :1234/:8080, free disk on C:, GPU VRAM via nvidia-smi |
| GET | `/models/installed` | `lms ls --json`, `*.gguf` under `%USERPROFILE%\.llamacpp`, loaded state from `/v1/models` |
| GET | `/models/search?q=&limit=` | HF GGUF search with file sizes, quant, and VRAM fit |
| POST | `/models/pull` | `{repo, file, engine: lmstudio\|llamacpp, quant, method?: direct\|lms}` returns `{id}` |
| GET | `/models/pull/:id/events` | SSE progress: `percent`, `mbps`, `eta` |
| DELETE | `/models/pull/:id` | cancel (removes the partial file) |
| POST | `/models/load`, `/models/unload` | `{id, engine}`: `lms load/unload`, or a managed `llama-server` (default port 8081, set `port`, `ctx`, `ngl` to change) |
| DELETE | `/models/:id` | delete a `.gguf` under `.llamacpp` |
| * | `/lmstudio/v1/*`, `/llamacpp/v1/*` | streaming passthrough. Returns 503 JSON if the engine is down |

Optional env overrides: `BRIDGE_PORT`, `LMSTUDIO_PORT`, `LLAMACPP_PORT`, `LLAMA_SERVER_PORT`,
`LLAMA_SERVER_PATH`, `LLAMACPP_DIR`, `LMSTUDIO_MODELS_DIR`, `HF_TOKEN`.
