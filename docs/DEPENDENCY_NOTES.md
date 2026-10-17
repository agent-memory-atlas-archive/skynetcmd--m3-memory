# Dependency notes

Why each pin is what it is. Declarations live in `pyproject.toml`;
`requirements.txt` is generated from them (`python bin/gen_requirements.py`,
resolved as a plain installer would, without uv's overrides) and `uv.lock` is
uv's lock of the same declarations plus the extras.

## Version-limit policy

Every limit is a decision, and every decision states its reason here or in a
comment on the requirement (tests/test_dependency_limits_documented.py).

- **Floor (`>=`)**: the oldest version m3 is known to work with, usually raised
  for a fix or CVE named in the package's section below. A floor is not a limit.
- **Default ceiling: below the floor's next major (`>=X.y,<X+1`; `<1` for a 0.x
  floor).** A major release may change the API; m3 adopts one deliberately, after
  the suite and the lane matrix are green on it, by raising the ceiling in its own
  change. This one rule is the reason for every ceiling of that exact shape, so
  those need no per-package note.
- **Anything else needs its own reason**: an exact pin (`==`, `~=`), an exclusion
  (`!=`), or a ceiling narrower or wider than the next major. Each has a section
  below or a comment naming the package on the requirement.

## Overview

M3 Memory — Unified Dependencies
Install: pip install -r requirements.txt

RUNTIME DEPENDENCY (not Python): M3 needs an OpenAI-compatible LLM server
running locally for embeddings and optional enrichment features.
  Required: an embedding model (e.g. qwen3-embedding:0.6b via Ollama).
  Optional: a small chat model (0.5B-1B instruct, e.g. qwen2.5:0.5b or
    llama3.2:1b) for auto-classification, summarization, and consolidation.
    These features are no-ops if no chat model is loaded.
See QUICKSTART.md and CONFIG_REFERENCE.md (`[llm]`) for runtime recommendations
across Ollama, LM Studio, llama.cpp --server, vLLM, and LocalAI.


## Core Engine & MCP

### `fastmcp` (`fastmcp>=3.4.7,<4`)

**Held below 4.** fastmcp 4.x requires `mcp>=2,<3`
(through `fastmcp-slim[client]`), and m3 ships `mcp>=1.30,<2`, so with 4.x in core
a resolving installer cannot install m3 at all. That shipped unnoticed for one
unpushed commit: uv resolved it only because `[tool.uv]` overrides mcp (for the
crewai extra), and the local venv was filled by `uv pip sync`, which installs
pins without resolving. The lane matrix's `pip install -r requirements.txt`
caught it. Measured: `fastmcp>=3.4.7,<4` with `mcp>=1.30,<2` resolves (3.4.8).

m3 does not import the standalone `fastmcp` package: its MCP server is
`mcp.server.fastmcp`, bundled in `mcp` (importing m3's MCP seam never loads
`fastmcp`). The dependency is kept on purpose, to adopt FastMCP features where
they simplify m3 (an m3 to-do). **fastmcp 4 comes with the mcp 2 raise below**,
which is a pin change plus a CI matrix entry; raise the two together.

`requirements.txt` generation checks that the core dependencies resolve WITHOUT
the uv overrides (bin/gen_requirements.py), so an override can never again mask a
conflict in what `pip install m3-memory` installs.

⚠ mcp <2 and fastmcp <4 are a CONSERVATIVE DEFAULT, not a hard incompatibility.
The code runs on BOTH majors: bin/mcp_compat.py owns the differences, and the
full suite is green on each (mcp 1.30.0: 4980 passed; mcp 2.2.0: 4545 passed,
measured 2026-09-17).

What 2.x actually changed, and where it is handled:
  * FastMCP renamed to MCPServer          -> mcp_compat resolves the import
  * Settings lost host/port/streamable_http_path
                                           -> mcp_compat.run_http passes them
                                              to run() instead
  * Settings lost transport_security       -> mcp_compat.{set,get}_transport_
                                              security + http_app(). SECURITY-
                                              RELEVANT: building the ASGI app
                                              without passing it leaves DNS-
                                              rebinding protection silently OFF.
The auth surface (AccessToken, AuthSettings, TransportSecuritySettings) and
tool()/run()/add_tool()/remove_tool()/list_tools() were unchanged.

The ceiling stays because 2.x is not yet the version this project ships and
tests in CI -- keeping the shipped surface to one known-good major. Raising it
is now a pin change plus a CI matrix entry, NOT a code migration. Anything
importing an mcp server class outside mcp_compat is caught by
tests/test_serve_auth.py::TestMcpImportSeam.

## Network & HTTP

### `urllib3` (`urllib3>=2.8.0,<3`)

2.7.0 fixes PYSEC-2026-141/142

## Database & Storage

## Security & Auth

### `defusedxml` (`defusedxml>=0.7.1,<1`)

Hardened XML parsing (XXE / entity-expansion defense) for
bin/doctor/schedule_probe.py, which parses `schtasks` XML output.

⚠ Declared in pyproject's base dependencies with the note "was only
transitively present (via fpdf2) — pinned directly so the safe path is
guaranteed on a fresh install", but it was MISSING here. This file is the
documented install path (`pip install -r requirements.txt`) and is what CI
installs, so that guarantee did not hold on either: defusedxml arrived only
because fpdf2 happens to require it. A security dep reached by luck is not
pinned. Keep both files in agreement.

## System & Thermal

## Intelligence & Vector

### `numpy` (`numpy>=2.5.3`)

CAP LIFTED 2026-09-17. It read: numpy 2.5.0's bundled stub uses the PEP 695
`type` statement, which mypy rejects when targeting python_version 3.11, and
named its own trigger -- "lift once we drop 3.11 support". The floor is now
3.12 (pyproject requires-python and [tool.mypy] python_version), where PEP 695
is valid syntax.

Verified rather than assumed: a clean venv with numpy 2.5.3 runs mypy over
bin/ at --python-version 3.12 with ZERO errors across all 281 source files.

That run did surface three real typing errors in memory/search_ranking.py --
2.5's stubs type ndarray precisely enough that mypy narrowed a name shared by
the numpy and pure-Python branches. Runtime was always correct on both paths;
the branches now bind separate names. Fixed in the same change.

### `chromadb` (not declared; transitive through the `[crewai]` extra)

Tolerated, not pinned. osv-scanner reports 8 advisories (HIGH and CRITICAL)
against chromadb 1.5.9 in `uv.lock`, with no fixed release (2026-10-16). It
arrives only through crewai, so `pip install m3-memory` without `[crewai]`
never installs it, and nothing in m3 imports it. When a fixed release
exists, add it as a floor in the `crewai` extra and here.

### `rapidfuzz` (`rapidfuzz>=3.14.6,<4`)

Fast fuzzy string matching for entity coalescing (token-order-aware, ~3-4x
faster than stdlib difflib). Clean MIT leaf wheel, no transitive deps. The
entity-coalesce pass falls back to difflib if absent, so this is a perf/quality
dep, not a hard requirement.

### `networkx` (`networkx>=3.6.1,<4`)

Topic clustering for the Memory Wiki (bin/wiki/cluster.py, `m3 wiki generate`).
Imported directly, no optional extra and no in-tree fallback — the pure-Python
clustering path was retired 2026-07-24 when the wiki became core.

⚠ Declared in pyproject's base dependencies but MISSING here, so on this
install path it arrived only as a TORCH transitive (`torch` requires
`networkx>=2.5.1`). Measured 2026-09-21: installing requirements.txt without
torch fails 14 wiki test files at collection with
`ModuleNotFoundError: No module named 'networkx'`. A core dep must not depend
on an optional ML stack to be present. Keep both files in agreement.

## Embedding Server

### `transformers` (`transformers>=5.17.0,<6`)

⚠ `sentence-transformers` and `torch` are NOT here. They are declared in
pyproject.toml under the `embed-server` EXTRA, which is where they belong:
nothing in m3's core or its test suite imports either one. `bin/embed_server.py`
does, but it is a standalone service that no test imports, and the reranker in
`memory/search.py` imports `sentence_transformers` lazily and raises a named,
actionable error when it is absent.

Pinning them here made this file demand ~2 GB of ML wheels to run a suite that
never touches them, and made CI's Python 3.15 lane die at dependency
resolution -- torch publishes no cp315 wheel -- so that lane tested torch's
release schedule rather than m3. `pip install m3-memory` was never affected;
the published package's 24 core dependencies have never included torch.

To work on the embed server or the reranker:
    pip install -r requirements.txt "m3-memory[embed-server]"

⚠ Removing `sentence-transformers` also removes torch TRANSITIVELY (it
requires `torch>=2.2`), so the two only come out together. Re-adding either one
re-adds the 3.15 blocker.

`transformers` stays. It was previously described as transitive via
sentence-transformers/peft; with those gone it is a DIRECT dependency, and a
deliberate one: `tests/test_token_budget.py::TestAgainstRealTokenizer` imports
it for ground-truth token counts against the bge-m3 tokenizer, and 4 tests pass
there today. It is guarded by `skipUnless`, so dropping it would not fail the
suite -- it would silently convert real coverage into a permanent skip (§3: a
check nobody sees reads as coverage while providing none). It also declares no
torch dependency of its own and supports >=3.10, so it installs on 3.15.

FLOOR RAISED TO 5.17.0 on 2026-09-17, clearing four of the five CVEs that the
previous `<5` ceiling forced us to accept:
  - PYSEC-2026-2288 (Trainer torch.load RCE)          fixed 5.0.0
  - PYSEC-2026-2289 (config.json _attn_implementation) fixed 5.3.0
  - PYSEC-2026-2290 (LightGlue model load RCE)         fixed 5.5.0
  - PYSEC-2026-3929 (save_pretrained path traversal)   fixed 5.10.0

WHY THE OLD CEILING IS GONE. It said: "transformers 5.x removed
`is_torch_fx_available`, which FlagEmbedding imports". Re-checked 2026-09-17
and the premise no longer holds:
  * the symbol IS removed in 5.x (verified on 5.1.0), but
  * FlagEmbedding is NOT a dependency of m3 -- it appears in no requirements
    file, is imported by no module under bin/ or m3_memory/, and is not
    installed. It survived only in this comment.
  * m3 never imports transformers DIRECTLY at all. Its entire surface is
    sentence-transformers' SentenceTransformer (embed_server.py:117) and
    CrossEncoder (memory/search.py:292), plus GLiNER, none of which touch the
    removed symbol.
  * sentence-transformers itself declares `transformers<6.0.0,>=4.41.0` -- it
    never wanted <5.
Verified in clean venvs: both 5.10.4 and 5.17.0 install and import both m3
surfaces cleanly.

ONE CVE REMAINS and is ACCEPTED:
  - PYSEC-2025-217 (X-CLIP checkpoint-conversion RCE) -- NO fixed version
    exists at any release. Requires converting an attacker-supplied X-CLIP
    checkpoint; m3 has no conversion path and only runs embedding inference on
    its own configured model.

torch was pinned `>=2.14.0,<3` in the core dependencies until 2026-09-22; see
the note at the top of this section for why it moved to the `embed-server` extra.

## Utilities

### `python-dotenv` (`python-dotenv>=1.2.2,<2`)

The floor is 1.2.2, not the newest release, so m3 installs beside tools that pin
it (Hermes 0.22 pins 1.2.2). OSV lists no advisory for 1.2.2 (checked 2026-10-17).

### `pywin32` (removed 2026-09-17)

REMOVED 2026-09-17 (unused, verified):
  pywin32 -- no module imports win32api/win32con/pythoncom/etc. The many
    `win32` hits in the tree are `sys.platform == "win32"` string compares,
    not the package. keyring (the plausible consumer) declares
    pywin32-ctypes, a DIFFERENT package it installs itself: verified in a
    clean venv that `pip install keyring` yields pywin32-ctypes only and
    WinVaultKeyring still resolves as the active backend.

### `tomlkit` (`tomlkit==0.15.1`)

m3_config.toml writer; pinned exactly, deliberately.

### `pillow` (`pillow>=12.3.0`)

fpdf2 declares `Pillow!=9.2.*,>=8.3.2` — a 2021 floor. Resolved to its LOWEST
satisfying version that is Pillow 9.5.0, carrying 37 known CVEs (osv-scanner
2.4.0 against this file, 2026-09-30; the finding is attributed to this file
with no Pillow line in it, because osv resolves transitively). `pip install`
takes the newest and is unaffected, so this guards the constrained-resolver
and stale-lockfile cases rather than the common one.

Nothing in m3 imports PIL. Pinned for the same reason as defusedxml above: a
security-relevant version that is correct only by luck is not pinned. Kept in
agreement with pyproject's base dependencies.

### `pypdf` (`pypdf>=6.19.0,<7`)

PDF ingestion backend for bin/files_memory/chunkers/pdf.py. UNDECLARED until
2026-09-17: the chunker probes PyMuPDF then pypdf and sets available=False if
neither is importable, so on a clean install PDFs fell back to the TEXT
chunker -- binary-ish extracted junk instead of pages. It only worked here
because unstructured-client happens to pull pypdf transitively, which is not
a dependency m3 controls.

pypdf (pure Python) is the declared floor; PyMuPDF is an OPTIONAL faster
path the chunker prefers when present, left undeclared because it ships
native wheels and an AGPL licence -- not something to force on every user.

### `sharepoint-to-text` (`sharepoint-to-text>=2.0.0,<3`)

Binary document ingestion for bin/files_memory/chunkers/office.py:
.docx .doc .pptx .ppt .xlsx .xls .rtf .epub .odt .odp .ods (35 MIME types).

ONE library rather than six. The per-format set would be python-docx +
python-pptx + openpyxl + xlrd + striprtf + ebooklib -- which still leaves the
LEGACY binaries (.doc/.xls/.ppt) without a trustworthy pure-Python parser, and
drags ebooklib's AGPL-3.0 licence into a distributed tool. sharepoint2text is
Apache-2.0, covers the legacy formats, and ships zip-bomb / path-traversal /
encrypted-file handling this chunker would otherwise write itself.

Heavier 'unified' options were rejected on footprint and privacy: docling
pulls torch and fetches model weights from HuggingFace at first run (runtime
network dependency); unstructured has 116 core deps and caps at <3.14;
markitdown pulls onnxruntime just to sniff file types.

### `beautifulsoup4` (`beautifulsoup4>=4.15.0,<5`)

HTML ingestion: bin/files_memory/chunkers/html.py. Parsed with bs4 +
stdlib html.parser (no lxml/html5lib). Was declared but unimported until
2026-09-17 -- .html/.htm resolved to filetype "html" with no registered
chunker, so HTML fell through to the text chunker and raw markup entered
the store as prose.

## MCP Proxy & API

## Development & Testing

### `types-setuptools` (`types-setuptools>=84.0.0`)

Tracks the setuptools floor below. The old <82 cap mirrored torch 2.11's
`setuptools<82`; torch 2.14 dropped that constraint entirely.

## Build Tools

### `twine` (`twine>=6.2.0,<7`)

HELD BELOW 7 on 2026-10-10. twine 7.0.0 requires `packaging>=26.1`, and
langchain-core 0.3.86 (the newest 0.3.x, which the `langchain` extra caps to)
requires `packaging<26`. uv.lock resolves the dev group and the extras together,
so twine 7 can only be locked by backtracking langchain-core to 0.3.76. That
release is merely uncapped, not known to work with packaging 26. The runtime
integration wins over a dev tool.

Releases are unaffected: `publish.yml` installs twine unpinned in its own
environment, and twine 7.0.0 `check --strict` passes on m3's sdist and wheel
(measured 2026-10-10).

Lift when the `langchain` extra moves to langchain-core 1.x: 1.6.9 no longer
caps packaging (see "Inherited limits" below).

### `setuptools` (`setuptools>=84.0.0`)

setuptools>=77 for the SPDX `license = "Apache-2.0"` form.
(Also pinned in pyproject.toml build-system.requires for isolated builds.)

⚠ KEEP THE SETUPTOOLS FLOOR WHERE IT IS. The reason it could be raised
was torch: 2.11 declared `setuptools<82`, which forced a ceiling here and with
it CVE-2026-59890; torch 2.14 declares `setuptools>=77.0.3` with no upper
bound, which is what unblocked the fix. That history is why the floor exists,
and it does not depend on torch being installed -- do not "simplify" it away
now that the pin is gone.

FLOOR RAISED TO 83.0.0 on 2026-09-17, clearing CVE-2026-59890 /
PYSEC-2026-3447. FileList matched MANIFEST.in exclude/prune globs without
Unicode normalization, so on macOS APFS/HFS+ an NFD filename could bypass an
NFC exclusion and get packed into an sdist.

The previous `<82` ceiling existed ONLY to stay inside torch 2.11's declared
`setuptools<82`, and that comment named its own revisit trigger: "revisit when
torch's setuptools<82 pin lifts". It has. Verified 2026-09-17: torch 2.14.0
declares `setuptools>=77.0.3` with NO upper bound, and a clean venv resolves
torch 2.14.0 + setuptools 84.0.0 + transformers 5.17.0 together with
`pip check` reporting no broken requirements.

Worth recording WHY torch capped it at all: torch imports setuptools in
exactly two modules -- utils/cpp_extension.py (compiling custom C++/CUDA ops)
and a dynamo backend registry. m3 uses neither; it only runs inference. So the
cap was never a constraint on m3's runtime, only on torch's build tooling.

This CVE was invisible until 2026-09-12: the DefectDojo pipeline's pip-audit
ran `find | head -1` and audited examples/mac-agent/requirements.txt ONLY, so
the root manifest was never scanned. Fixed in scan_repo.py (audits all three;
261 packages, was ~8).

## Entity NER (optional extra)

### `gliner` (`entity-ner` extra)

Measured 2026-10-09: every gliner release requires an older transformers
(0.2.26: <5.2.0; 0.2.27: <5.7.0; 0.2.28: <5.14.0; 0.2.29 and later: <5.17.0).
m3's own floor is transformers>=5.17.0 (see `transformers` above: it clears the
CVEs listed there). So `entity-ner` and `embed-server` cannot be installed
together, and an `entity-ner` install runs a transformers below that floor.
`bin/m3_entities_gliner.py` warns on every run when that is the case, and
pyproject declares the two extras (and the test groups) as conflicting to uv.
Until gliner widens its range: run GLiNER only on trusted models and data.

## Inherited limits (set by dependencies, not by m3)

m3's own limits are above. These are the limits m3's dependencies put on
OTHER packages, which hold an install below the newest release no matter what
m3 declares. Measured 2026-10-10 from PyPI: each package's `requires_dist` at its `uv.lock`
version AND at its newest non-yanked final release that day, so the tables show
whether upgrading the dependent lifts a limit. Only limits that bind (the newest release falls outside them) are
listed. Re-measure before lifting anything here: a newer release of the
dependent package is what removes each one.

No section here uses a `name` heading on purpose: these are not m3's limits,
and tests/test_dependency_limits_documented.py reads those headings as m3's.

### Effective floors higher than m3 declares

m3's stated floor is not reachable in practice where a core dependency needs
more:

| Package | m3 declares | Needed by | Effective floor |
|---|---|---|---|
| pydantic | `>=2.11,<3` | fastmcp-slim 3.4.8 (core) | 2.11.7 |
| pydantic | `>=2.11,<3` | pydantic-ai-slim / pydantic-graph 2.55 (`pydantic-ai` extra) | 2.12 with that extra |
| starlette | `>=0.46` | sse-starlette 3.5.0 (core, via mcp) | 0.49.1 |
| langchain-core | `>=1.4.0,<2` | langgraph 1.2.x, langgraph-prebuilt 1.1 (`langchain` extra) | 1.6.10 locked; floor set 1.4.0 tested |
| packaging | `>=20.9` (`entity-ner`) | build, twine, deptry, pytest, langsmith, crewai | 23.2 to 24.0 in dev and extra installs |

### Dependents, their versions, and the limits they set

Each row is one dependent at its locked and its newest version; a limit is
listed only while it binds (the limited package's newest release falls
outside it). Regenerate both measurements before acting on a row.

#### `langchain` extra

| Dependent | Locked | Limits it sets (locked) | Newest | Limits it sets (newest) | m3 admits newest |
|---|---|---|---|---|---|
| langchain-core | 0.3.86 | packaging `<26.0.0,>=23.2.0`<br>uuid-utils `<1.0,>=0.12.0` | 1.6.9 | uuid-utils `<1.0,>=0.12.0` | blocked by m3 `<1,>=0.3.0` |
| langgraph | 0.6.11 | langgraph-checkpoint `<4.0.0,>=2.1.0`<br>langgraph-prebuilt `<0.7.0,>=0.6.0`<br>langgraph-sdk `<0.3.0,>=0.2.2` | 1.2.14 | none binding | blocked by m3 `<1,>=0.2.0` |
| langgraph-prebuilt | 0.6.5 | langgraph-checkpoint `<4.0.0,>=2.1.0` | 1.1.0 | none binding | yes |
| langsmith | 0.14.7 | uuid-utils `<1.0,>=0.12.0` | 0.14.7 (= locked) | same | yes |

#### `crewai` extra

| Dependent | Locked | Limits it sets (locked) | Newest | Limits it sets (newest) | m3 admits newest |
|---|---|---|---|---|---|
| crewai | 1.15.27 | aiofiles `~=24.1.0`<br>aiosqlite `~=0.21.0`<br>instructor `<1.16,>=1.3.3`<br>json-repair `~=0.60.1`<br>json5 `~=0.10.0`<br>lancedb `<0.30.1,>=0.29.2`<br>mcp `~=1.28.1`<br>openai `<3,>=2.30.0`<br>portalocker `~=2.7.0`<br>pydantic `<2.13,>=2.11.9`<br>regex `~=2026.1.15`<br>tomli `~=2.0.2`<br>tomli-w `~=1.1.0` | 1.15.27 (= locked) | same | yes |
| crewai-core | 1.15.27 | portalocker `~=2.7.0`<br>pydantic `<2.13,>=2.11.9`<br>tomli `~=2.0.2` | 1.15.27 (= locked) | same | yes |
| crewai-cli | 1.15.27 | pydantic `<2.13,>=2.11.9`<br>tomli `~=2.0.2`<br>tomli-w `~=1.1.0`<br>uv `~=0.11.6` | 1.15.27 (= locked) | same | yes |
| instructor | 1.15.4 | jiter `<0.15,>=0.6.1`<br>openai `<3.0.0,>=2.0.0`<br>rich `<15.0.0,>=13.7.0` | 1.17.0 | aiohttp `<3.14.0,>=3.9.1`<br>jiter `<0.15,>=0.6.1`<br>requests `<2.33.0,>=2.32.3`<br>rich `<15.0.0,>=13.7.0` | yes |

#### Embedding and NER stack

| Dependent | Locked | Limits it sets (locked) | Newest | Limits it sets (newest) | m3 admits newest |
|---|---|---|---|---|---|
| gliner | 0.2.29 | transformers `<5.17.0,>=4.51.3` | 0.2.29 (= locked) | same | yes |
| transformers | 5.16.1 | huggingface-hub `<2.0,>=1.5.0` | 5.19.0 | none binding | yes |
| sentence-transformers | 5.7.0 | none binding | 6.1.0 | huggingface-hub `<2.0.0,>=1.3.0` | blocked by m3 `<6,>=5.7.0` |
| torch | 2.14.1 | cuda-toolkit `==13.0.3`<br>nvidia-cudnn-cu13 `==9.24.0.43`<br>nvidia-cusparselt-cu13 `==0.8.1`<br>nvidia-nccl-cu13 `==2.30.7`<br>nvidia-nvshmem-cu13 `==3.4.5` | 2.14.1 (= locked) | same | yes |
| sympy | 1.14.0 | mpmath `<1.4,>=1.1.0` | 1.14.0 (= locked) | same | yes |

#### Core and other

| Dependent | Locked | Limits it sets (locked) | Newest | Limits it sets (newest) | m3 admits newest |
|---|---|---|---|---|---|
| fastmcp | 3.4.8 | fastmcp-slim `==3.4.8` | 4.1.0 | none binding | blocked by m3 `<4,>=3.4.7` |
| mcp | 1.30.0 | none binding | 2.3.0 | none binding | blocked by m3 `<2,>=1.30.0` |
| pydantic | 2.12.5 | pydantic-core `==2.41.5` | 2.14.0 | none binding | yes |
| twine | 6.2.0 | none binding | 7.0.0 | none binding | blocked by m3 `<7,>=6.2.0` |

### What follows from the tables

- **langchain-core 1.x drops `packaging<26`** (0.3.86 sets it, 1.6.9 does not),
  which is what holds `twine` below 7. Only m3's own `<1` keeps the extra on
  0.3; adopting 1.x is an API migration. `uuid-utils<1` remains in 1.6.9.
- **langgraph 1.x drops all three 0.6 caps** (checkpoint, prebuilt, sdk). Only
  m3's own `<1` blocks it.
- **crewai's newest is the locked 1.15.27**: every crewai pin, including
  `pydantic<2.13` and `mcp~=1.28.1`, stays until crewai ships a release. m3's
  `[tool.uv] override-dependencies` lifts only the mcp pin.
- **instructor 1.17 would add `aiohttp<3.14` and `requests<2.33`.** Both bind
  today, but crewai's `instructor<1.16` keeps 1.17 out.
- **sentence-transformers 6 would add `huggingface-hub<2`** even though the
  newest transformers lifts that cap. m3's own `<6` blocks it for now.
- **gliner 0.2.29 is its newest**, so `transformers<5.17` stays (see `gliner`).
- **m3's own default ceilings block these newest releases:** fastmcp 4,
  mcp 2, langchain-core 1, langgraph 1, sentence-transformers 6, twine 7.
  Each is adopted deliberately in its own change (Version-limit policy).
- torch's CUDA wheel pins and pydantic's pydantic-core pin are exact by design:
  they move with their parent.
