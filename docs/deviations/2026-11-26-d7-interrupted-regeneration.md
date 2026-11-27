---
date: 2026-11-26
faults_sha256: 81f02e436c4842a30bbca7c7780cf5748c61a83ed6f9e1f32372ba1f67a7ff01
baseline:
  model_sha256: d7efb072e7724d25048a4fda0a3e10b04bdef5d06b1403a1c93bd9f1240a63c8
  config_hash: 5c1277b1db8a99d27b529ae10370372e4da22ad6134478f18dcd8aae19d75d49
---

# Interrupted D7 regeneration, 26 Nov 2026

The first post-demo implementation launched a regeneration job on WSL host
`Sankalps-Razer` in tmux socket `driftgate-d7`. The job regenerated the D3 reference
and computed its D7 embeddings in `reference.json`. It started the L3 A/A timing
trial, which used llama-server PID 29368. The process group was stopped during that
trial. The preserved directory has the staged A/A bundle and the timing stage logs,
with no completed timing or numbered A/A measurement. No fault run was generated
and no D7 score for a fault was computed or printed.

The original directory `runs/2026-11-26-d7-matrix/` remains on disk as evidence.
Its regenerated reference and all its other files are excluded from the offline
calibration, fault scores and matrix.
