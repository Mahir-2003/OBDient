# OBDex snapshot

- Repository: https://github.com/foerbsnavi/OBDex
- Commit: `9ba1ffa095f3140d9e21b985b322ddea3134bb60` (2026-11-07), pinned as `OBDEX_COMMIT` in `src/obdient/config.py`
- Copied on 2026-11-13, unmodified:
  - `data/generic/*.yaml` → `generic/` (7 family files, 9,533 codes)
  - `data/pids/mode01.yaml`, `data/pids/mode09.yaml` → `pids/`
  - `LICENSE-DATA` (CC0-1.0, covers the data) and `LICENSE-CODE` (MIT, covers the upstream tools, none of which are copied)
- Not copied: `tools/`, `schema/`, the README and repository metadata.

To update: change `OBDEX_COMMIT`, copy the same paths from the new commit, and change the
expected code count in `src/obdient/knowledge/obdex.py`. The index file name embeds the commit,
so it is rebuilt on the next run.
