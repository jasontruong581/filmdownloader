---
phase: 6
title: Tests, docs, packaging
status: planned
priority: P1
effort: 6h
dependencies: [1a, 1b, 2, 3, 4, 5]
order: 7
---

# Phase 6: Tests, docs, packaging

## Context

Earlier phases each carry their own tests. This phase closes the seams: deletes
the compatibility shims, gets the whole suite green from a clean checkout,
records the new architecture, and makes the tool installable and runnable in one
documented sequence on both platforms.

## Requirements

- Functional: a clean checkout reaches a working UI through a documented command
  sequence, on Windows and on Linux.
- Functional: the phase-1a re-export shims are removed and every caller imports
  from its real module.
- Non-functional: the full suite runs with no network, no Chrome, no FFmpeg, no
  yt-dlp extraction, and no built frontend.
- Non-functional: no test fixture contains a real media URL, a cookie, a session
  token, or copyrighted content.
- Non-functional: docs describe the resolver chain, the executor split, the batch
  capability tiers, the job lifecycle, the API surface, and the security posture.

## Related code files

- Delete: the shim modules left at `src/videotrack/models.py`, `resolvers.py`,
  `detect.py`, `download.py`, `capture.py`, `io.py`, `static_player.py`,
  `collection.py`, `quatvn.py`
- Modify: `README.md`, `docs/architecture.md`, `pyproject.toml`,
  `requirements.txt`, `.gitignore`, `main.py`, `batch_run_csv.py`, `tests/`
- Create: `docs/api.md`, `docs/batch-capability.md`, `.env.example`,
  `.github/workflows/ci.yml`
- Create: `tests/test_site_registry.py`

## Implementation steps

1. Remove the shims and update every import. `grep -rn "from videotrack import"`
   plus a grep for the old relative shim paths must come back empty.
2. Add `tests/test_site_registry.py`: registering two plugins routes each URL to
   the right one; an unmatched URL returns nothing; `plugin_for_kind` routes
   postprocessing; the crawl preset lookup falls back to `generic`;
   `import videotrack.sites` succeeds with Selenium absent.
3. Add `.env.example` documenting `FILMDOWNLOADER_SCRATCH`, `FILMDOWNLOADER_DB`,
   `FILMDOWNLOADER_TOKEN`, `FILMDOWNLOADER_OUTPUT`, `FILMDOWNLOADER_FFMPEG`, and
   the bind host and port. `.env` is already gitignored and `.env.example`
   already allowed.
4. `pyproject.toml`: keep `filmdownloader = videotrack.cli:main`, add
   `filmdownloader-server = videotrack.server.__main__:main`, keep the `server`
   extra, add a `dev` extra.
5. `.github/workflows/ci.yml`: matrix on Python 3.10 and 3.12; install with the
   dev extra; run the unittest suite; then set up Node, `npm ci`,
   `npm run gen:api` against the **dumped `openapi.json`** (no server started),
   fail if the regenerated types differ from the committed file, then
   `npm run build`.
6. Rewrite `README.md`:
   - unchanged responsible-use statement;
   - requirements split into always required (Python, FFmpeg), needed only for
     the browser fallback (Chrome, ChromeDriver), and needed only for the quatvn
     plugin (ImageMagick), with a note that FFmpeg is frequently not on PATH on
     Windows and `FILMDOWNLOADER_FFMPEG` exists for that;
   - install, then `python main.py doctor` as the first thing to run;
   - web UI quick start: build the frontend once, then run the server;
   - CLI reference for `--engine`, `--format`, `--list-formats`,
     `--probe-batch`, `queue`, and `doctor`, noting `--resolver` is a retained
     alias;
   - batch: the tier model in two sentences, linking `docs/batch-capability.md`;
   - yt-dlp needs regular updating; `doctor` prints the installed version;
   - the loopback-default, token-on-wider-bind, and `Host`-guard posture;
   - the old-to-new import mapping for anyone with a local script.
7. Rewrite `docs/architecture.md` around the layers (core, engines, sites, jobs,
   server, web), the resolver chain, the two-executor split with the normalized
   event contract, and the job lifecycle. The current module list describes the
   pre-refactor layout and must be replaced, not appended to.
8. Add `docs/batch-capability.md`: the four tiers, what each probe actually
   checks, the bounds on each probe, and an explicit statement that a probe
   proves enumeration and not downloadability. This is the document a future
   contributor reads before adding a fifth detector.
9. Add `docs/api.md`: each endpoint, its request and response shape, and the SSE
   event kinds. Link `/docs` for the generated schema rather than duplicating
   field tables.
10. Update this plan's phase statuses and tick the index acceptance criteria as
    each lands.

## Validation

- Clean clone, then: create a venv, `pip install -e ".[server,dev]"`,
  `python main.py doctor`, `cd web && npm ci && npm run build`,
  `python -m videotrack.server --open-browser`. The UI loads. Repeat on Linux.
- `python -m unittest discover -s tests -v` passes with Chrome, FFmpeg, yt-dlp
  extraction, and the built frontend all absent.
- `grep -rniE 'vlxx|quatvn|xvideo|flowplayer|video-code|actress-tag|tiktokcdn|bytecdn' src/videotrack/core src/videotrack/engines src/videotrack/jobs src/videotrack/server`
  returns nothing. The `tiktokcdn|bytecdn` terms matter: they are in
  `detect.py:49-50` today and the original grep would have missed them.
- `grep -rn "yt_dlp" src/videotrack/core/` returns nothing.
- `grep -rniE 'https?://[a-z0-9.-]+\.(com|net|org|my)' tests/` returns only
  `.example`, `.test`, and `.invalid` hosts.
- Every link in `README.md`, `docs/architecture.md`, `docs/api.md`, and
  `docs/batch-capability.md` resolves to a file that exists.
- CI green on both Python versions.

## Risk

Deleting the shims can break an unversioned local script. Mitigation: the
deletion is its own commit and the README records the old-to-new import mapping,
making a local script a one-line fix.

## Rollback

Documentation and test commits are independently revertible. The shim deletion is
one commit; reverting it restores the old import paths.
