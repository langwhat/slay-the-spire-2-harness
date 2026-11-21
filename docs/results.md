# Results

## Fool, Ascension 10, 20 new seeds (2026-11-20)

**13 wins, 7 losses: 65%.** Every run was played from the start to the game's own victory or defeat.

- **Wins:** 01–08, 13, 14, 17–19. All cleared floor 49.
- **Losses:** 09, 10, 11, 12, 15, 16, 20. Four lost on floor 49, and the other three on floors 11, 17, and 33.

With 20 runs, the Wilson 95% interval for the win rate is 43%–82%.

### Setup

| Item | Value |
| --- | --- |
| Game | Slay the Spire 2 v0.113.1 (macOS), Standard mode |
| Character and difficulty | Fool, Ascension 10 |
| Core | [`f0486a9`](https://github.com/langwhat/slay-the-spire-2-harness/commit/f0486a95c4eb005b0d91386dd534d063c61df9ab) |
| Mod | Built from the same source; `sts2agent.dll` SHA-256 `abfced68f861c125e215ee8fef21c0227f545465820956d267726627a75284d0` |
| Knowledge | [`sts2-knowledge` at `bdf5079`](https://github.com/langwhat/sts2-knowledge/tree/bdf5079aa6a71394f07d6a91a068302becb81e97) |
| Model | `gpt-6.2-astra`, Responses protocol, reasoning effort `xhigh`, service tier `default`, context window 300,000 |
| Mode | Ordinary mode (`STS2_MINIMAL=false`), builtin driver, no viewer input |
| Launch | `sts2 headless`, one isolated game instance per run |

The batch ran from the core at `f0486a9` plus an evaluation-only patch, which was later committed as `be09282`. The patch touched `src/sts2_harness_eval` and the Mod's developer checkpoint code. It did not touch `src/sts2_harness`, so gameplay behavior is that of `f0486a9`.

### How the seeds were chosen

- The 20 seeds were frozen in a written protocol before any of them was played. They were new seeds, not used during development.
- They served as the unchanged baseline arm of a planned paired comparison. The candidate arm was never run.
- The results stayed sealed until all 20 runs had finished.
- Before release, each result was checked against the archive's recorded seed, character, and ascension, and against the game's own run history file.

### Per-run results

The game normalizes the letters `I` and `O` in a seed to the digits `1` and `0`. The "as played" column is the seed the game actually used; enter it to replay the same seed.

| # | Seed as entered | Seed as played | Result | Final floor | Final HP |
| --- | --- | --- | --- | --- | --- |
| 01 | `C0DXV1SEH0BM` | `C0DXV1SEH0BM` | Win | 49 | 36 |
| 02 | `T3ZX6GPYQ3CW` | `T3ZX6GPYQ3CW` | Win | 49 | 15 |
| 03 | `RRQZ7NFVF7FU` | `RRQZ7NFVF7FU` | Win | 49 | 57 |
| 04 | `DW7EWVRDDPTP` | `DW7EWVRDDPTP` | Win | 49 | 45 |
| 05 | `LPHEHMCQTC36` | `LPHEHMCQTC36` | Win | 49 | 42 |
| 06 | `Z7VVLHWKQZKJ` | `Z7VVLHWKQZKJ` | Win | 49 | 48 |
| 07 | `R4D4F3G1DCI8` | `R4D4F3G1DC18` | Win | 49 | 62 |
| 08 | `RUBX2O60M76S` | `RUBX2060M76S` | Win | 49 | 71 |
| 09 | `ZDR8ZQEFVVDG` | `ZDR8ZQEFVVDG` | Loss | 33 | 0 |
| 10 | `EFASQDOT1IH5` | `EFASQD0T11H5` | Loss | 49 | 0 |
| 11 | `PUQMC1CYQQNI` | `PUQMC1CYQQN1` | Loss | 49 | 0 |
| 12 | `CTVG3HF5QTBR` | `CTVG3HF5QTBR` | Loss | 17 | 0 |
| 13 | `DV7H9SP8CJDV` | `DV7H9SP8CJDV` | Win | 49 | 49 |
| 14 | `FZZLWLEZU1VB` | `FZZLWLEZU1VB` | Win | 49 | 36 |
| 15 | `1XSSMSMYHKEG` | `1XSSMSMYHKEG` | Loss | 11 | 0 |
| 16 | `LKDHCMWUWT2G` | `LKDHCMWUWT2G` | Loss | 49 | 0 |
| 17 | `OLMHJT09JU1C` | `0LMHJT09JU1C` | Win | 49 | 29 |
| 18 | `QUVTKDOV1PCR` | `QUVTKD0V1PCR` | Win | 49 | 77 |
| 19 | `FIMQIZAK2F4G` | `F1MQ1ZAK2F4G` | Win | 49 | 24 |
| 20 | `D7KJBUGOVHR8` | `D7KJBUG0VHR8` | Loss | 49 | 0 |

### Technical notes

- **Runs 01–04.** The first launches exited during Python import, before the game started or any model request was sent. They were relaunched once with the same seeds and materials. Each counted result is the relaunched run.
- **Runs 07, 08, 10, and 11.** The first readback compared the entered seed text with the game's normalized seed and reported a mismatch. The readback was corrected to apply the game's normalization. The runs themselves were not changed or repeated, and the correction was recorded before the results were revealed.
- **Other runs.** No run was interrupted, replaced, or excluded.

### Evidence

- **Run records.** Archives, logs, native run histories, the frozen protocol, and the seed normalization record are in [`sts2-harness-runs/2026-11-20-fool-a10`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-20-fool-a10).
- **Inspecting an archive.** Use `uv run --python 3.14 sts2 journal --archive <run>/instance/archive.sqlite` with the core at `f0486a9` (archive schema 21).
- **Knowledge.** The documents delivered during these runs come from [`sts2-knowledge`](https://github.com/langwhat/sts2-knowledge) at `bdf5079`.
