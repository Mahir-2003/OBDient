# Wal33D dtc-database, Volkswagen subset

- Repository: https://github.com/Wal33D/dtc-database
- Commit: `04c43d72e7db7197658b6f72fe582c5076d9eee8` (2026-02-16), pinned as `WAL33D_COMMIT` in `src/obdient/config.py`
- License: MIT (`LICENSE`, copied unmodified)

## Extraction steps (2026-11-13)

1. Cloned the repository and checked out the commit above.
2. Copied `data/source-data/volkswagen_codes.txt` to `volkswagen_codes.txt`, unmodified. It has
   528 lines of the form `P1xxx - Description` (P1101 to P1866), with no duplicate codes.
3. Built `vw.json` from it: a `{code: description}` map, keys sorted, one entry per line of the
   source, descriptions unchanged.

Nothing else was copied. In particular `other_codes.txt` (the `OTHER` bucket), the other makes,
the generic `p_codes.txt` / `b_codes.txt` / `c_codes.txt` / `u_codes.txt` files and the prebuilt
`dtc_codes.db` are **not** vendored and must not be: most VW codes collide with `OTHER` under a
different meaning.

These are unverified community definitions with no sources. OBDient shows them only for
Volkswagen vehicles, labelled as such, and never bases a diagnosis on one alone (SPEC §5.3).
