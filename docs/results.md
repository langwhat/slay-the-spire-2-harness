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

This Python code regenerates the 20 entered seeds in run order, 01–20. The
output matches [`protocol.json`](https://github.com/langwhat/sts2-harness-runs/blob/main/2026-11-20-fool-a10/protocol.json)
on Python 3.9 and 3.14.

```python
from random import Random
from string import ascii_uppercase, digits

r = Random("sts2-fool-a10-holdout-v1-2026-11-20")
seeds = ["".join(r.choices(ascii_uppercase + digits, k=12)) for _ in range(20)]
```

The local machine recorded protocol creation at `2026-11-20T13:42:01Z` and the
first counted run starting at `2026-11-20T13:43:47Z`. The seeds were set aside
from development for the unchanged baseline arm of a planned paired comparison.
Only the baseline arm ran, and the results stayed sealed until all 20 runs finished.
These timestamps come from the local clock. The available evidence provides no
independent timestamp and cannot establish whether other batches were run.

To spot-check a seed, start a Fool Ascension 10 Standard run in v0.113.1 with the
**as played** seed and a clean, fully unlocked starting profile, as described in
[the native template setup](running.md#developer-headless-runs). Compare the opening
Ancient's floor 1 choices and map with the archive's initial observation. To
check later encounters against the archive and the game's `saves/history/*.run`,
make the same preceding choices as the recorded run. Each recorded result was
checked against its archive and native run history.

For future batches, publish the protocol before starting and retain the hosting
service's server-side push time, or commit to a future public randomness beacon
round, such as a drand round, and use its output as the seed-generation label.

### Per-run results

The game normalizes the letters `I` and `O` in a seed to the digits `1` and `0`. The "as played" column is the seed the game actually used; enter it to replay the same seed.

Request counts come from the archives' model records, read with the recording
revision `f0486a9`. Token totals sum provider-reported usage across attempts.
Cached input is included in input tokens, and reasoning is included in output
tokens. Run 01 has one extra attempt with no reported usage. Elapsed time is
the difference between the start and end timestamps in each `result.json`,
rounded to the nearest second.

| # | Seed as entered | Seed as played | Result | Final floor | Final HP | Requests | Input tokens | Cached input | Output tokens | Reasoning tokens | Elapsed |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 01 | `C0DXV1SEH0BM` | `C0DXV1SEH0BM` | Win | 49 | 36 | 398 | 10,304,893 | 8,752,128 | 77,939 | 28,121 | 91:09 |
| 02 | `T3ZX6GPYQ3CW` | `T3ZX6GPYQ3CW` | Win | 49 | 15 | 282 | 5,730,145 | 4,476,544 | 62,090 | 23,341 | 71:26 |
| 03 | `RRQZ7NFVF7FU` | `RRQZ7NFVF7FU` | Win | 49 | 57 | 312 | 7,244,884 | 5,915,008 | 66,658 | 27,400 | 71:27 |
| 04 | `DW7EWVRDDPTP` | `DW7EWVRDDPTP` | Win | 49 | 45 | 353 | 9,182,718 | 7,576,320 | 72,412 | 27,719 | 79:49 |
| 05 | `LPHEHMCQTC36` | `LPHEHMCQTC36` | Win | 49 | 42 | 262 | 5,486,603 | 4,311,424 | 61,959 | 27,473 | 71:17 |
| 06 | `Z7VVLHWKQZKJ` | `Z7VVLHWKQZKJ` | Win | 49 | 48 | 278 | 5,893,604 | 4,593,920 | 67,642 | 29,273 | 77:55 |
| 07 | `R4D4F3G1DCI8` | `R4D4F3G1DC18` | Win | 49 | 62 | 394 | 13,923,146 | 12,164,608 | 83,422 | 35,463 | 98:06 |
| 08 | `RUBX2O60M76S` | `RUBX2060M76S` | Win | 49 | 71 | 330 | 8,158,793 | 6,662,144 | 64,027 | 23,851 | 74:21 |
| 09 | `ZDR8ZQEFVVDG` | `ZDR8ZQEFVVDG` | Loss | 33 | 0 | 195 | 4,012,679 | 3,244,800 | 43,030 | 18,261 | 48:34 |
| 10 | `EFASQDOT1IH5` | `EFASQD0T11H5` | Loss | 49 | 0 | 345 | 8,494,508 | 7,104,000 | 72,099 | 29,558 | 80:27 |
| 11 | `PUQMC1CYQQNI` | `PUQMC1CYQQN1` | Loss | 49 | 0 | 263 | 5,619,299 | 4,369,920 | 56,755 | 22,220 | 61:52 |
| 12 | `CTVG3HF5QTBR` | `CTVG3HF5QTBR` | Loss | 17 | 0 | 92 | 1,440,580 | 1,100,160 | 20,033 | 8,194 | 20:54 |
| 13 | `DV7H9SP8CJDV` | `DV7H9SP8CJDV` | Win | 49 | 49 | 243 | 5,047,639 | 3,908,096 | 50,600 | 18,172 | 48:27 |
| 14 | `FZZLWLEZU1VB` | `FZZLWLEZU1VB` | Win | 49 | 36 | 326 | 7,682,409 | 6,288,256 | 65,637 | 25,364 | 71:01 |
| 15 | `1XSSMSMYHKEG` | `1XSSMSMYHKEG` | Loss | 11 | 0 | 54 | 793,460 | 626,688 | 9,170 | 2,400 | 10:16 |
| 16 | `LKDHCMWUWT2G` | `LKDHCMWUWT2G` | Loss | 49 | 0 | 296 | 6,836,894 | 5,486,208 | 68,645 | 31,555 | 65:44 |
| 17 | `OLMHJT09JU1C` | `0LMHJT09JU1C` | Win | 49 | 29 | 339 | 8,754,478 | 7,217,280 | 65,418 | 24,836 | 69:44 |
| 18 | `QUVTKDOV1PCR` | `QUVTKD0V1PCR` | Win | 49 | 77 | 230 | 4,852,035 | 3,724,288 | 61,917 | 29,777 | 56:17 |
| 19 | `FIMQIZAK2F4G` | `F1MQ1ZAK2F4G` | Win | 49 | 24 | 292 | 6,473,442 | 5,141,248 | 72,816 | 33,380 | 67:53 |
| 20 | `D7KJBUGOVHR8` | `D7KJBUG0VHR8` | Loss | 49 | 0 | 338 | 7,616,581 | 6,235,776 | 83,733 | 39,243 | 79:17 |

### Technical notes

- **Runs 01–04.** The first launches exited during Python import, before the game started or any model request was sent. They were relaunched once with the same seeds and materials. Each counted result is the relaunched run.
- **Runs 07, 08, 10, and 11.** The first readback compared the entered seed text with the game's normalized seed and reported a mismatch. The readback was corrected to apply the game's normalization. The runs themselves were not changed or repeated, and the correction was recorded before the results were revealed.
- **Other runs.** No run was interrupted, replaced, or excluded.

### Evidence

- **Run records.** Archives, logs, native run histories, the frozen protocol, and the seed normalization record are in [`sts2-harness-runs/2026-11-20-fool-a10`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-20-fool-a10).
- **Inspecting an archive.** Use `uv run --python 3.14 sts2 journal --archive <run>/instance/archive.sqlite` with the core at `f0486a9` (archive schema 21).
- **Knowledge.** The documents delivered during these runs come from [`sts2-knowledge`](https://github.com/langwhat/sts2-knowledge) at `bdf5079`.
