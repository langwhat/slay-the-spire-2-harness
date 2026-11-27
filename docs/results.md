# Results

## Six-character rotation, Ascension 10, 36 consecutive runs (2026-11-26 to 2026-11-27)

**21 wins, 15 losses: 58.3%.** Best streak: **9 consecutive wins** (Runs 25–33, spanning all six characters plus three more wins). Every run played from start to the game's own victory or defeat without save/load, external strategy guides, or viewer input.

With 36 runs, the Wilson 95% confidence interval for the win rate is **42%–73%**.

### By character

| Character | Wins | Losses | Win rate |
| --- | ---: | ---: | ---: |
| Defect | 5 | 1 | 83.3% |
| Silent | 4 | 2 | 66.7% |
| Regent | 4 | 2 | 66.7% |
| Necrobinder | 4 | 2 | 66.7% |
| Ironclad | 3 | 3 | 50.0% |
| Fool | 1 | 5 | 16.7% |

### Setup

| Item | Value |
| --- | --- |
| Game | Slay the Spire 2 v0.114.0 (`a7b9a9c0`, Windows, Standard mode) |
| Characters and difficulty | Fixed 6-character rotation (`IRONCLAD`, `SILENT`, `REGENT`, `NECROBINDER`, `DEFECT`, `FOOL`), 6 rounds = 36 runs, Ascension 10 |
| Core | [`44cd9b1`](https://github.com/langwhat/slay-the-spire-2-harness/commit/44cd9b1a68bdff175c92a7ca643fc3538e4b1957) for Runs 12–36 (25 runs: 13W–12L); Runs 01–11 (11 runs: 8W–3L) ran across earlier ancestors on the same branch during live deployment updates (`16f4447`, `3291601`, `52f2244`, `df68e80`, `4302239`, `80312b7`) |
| Mod | `sts2agent` built from the matching core revision |
| External knowledge | None (`entry_documents_enabled=false`) |
| Model | `gpt-6.2-astra`, Responses protocol, reasoning effort `xhigh`, context window 300,000 |
| Mode | Minimal mode (`STS2_MINIMAL=true`), built-in driver, no viewer input (0 viewer messages delivered) |
| Launch | Live Windows GUI session with automated run cycling on a veteran fully-unlocked profile (66 epochs; no new epochs unlocked during the batch) |

### Usage and timing summary

- **Totals across 36 runs:** 29.95 hours of in-game run time, 217,695,094 input tokens (187,244,672 cached, **86.0%** overall cache hit rate), 1,494,302 output tokens (including 755,483 reasoning tokens).
- **All 36 runs (median):** 50.0 minutes, 6.16M input tokens (5.26M cached), 43.1K output tokens.
- **21 winning runs (median):** 56.3 minutes, 7.17M input tokens (6.15M cached), 45.3K output tokens.

### Per-run results

Runs are listed in chronological order. Token totals sum provider-reported usage across all model attempts recorded in each run's SQLite archive (cached input is included in input tokens; reasoning is included in output tokens). Final floor, final HP, defeating encounter, and elapsed time come from each run's native `saves/history/*.run` file and were cross-checked against the core archive.

| # | Character | Seed | Result | Floor | HP | Defeated by | Core | Requests | Input tokens | Cached input | Output tokens | Reasoning | Elapsed | Files |
| --- | --- | --- | --- | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 01 | Ironclad | `CSWSC29RD8F6` | Loss | 33 | 0 | `ENCOUNTER.THE_INSATIABLE_BOSS` | `16f4447` | 138 | 2,287,949 | 1,875,712 | 24,601 | 13,081 | 39:44 | [`01-ironclad/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/01-ironclad) |
| 02 | Silent | `G7PXZV3VGUC0` | Win | 49 | 6 | — | `16f4447` | 238 | 6,092,401 | 5,200,896 | 34,396 | 14,916 | 56:18 | [`02-silent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/02-silent) |
| 03 | Regent | `021C3Z3A1880` | Win | 49 | 28 | — | `3291601` | 276 | 7,479,777 | 6,314,240 | 52,490 | 28,159 | 72:10 | [`03-regent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/03-regent) |
| 04 | Necrobinder | `GGMSN6P0G6W1` | Win | 49 | 9 | — | `52f2244` | 296 | 8,263,786 | 7,199,616 | 50,339 | 25,743 | 60:41 | [`04-necrobinder/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/04-necrobinder) |
| 05 | Defect | `6RYMUBWKVY6F` | Win | 49 | 73 | — | `52f2244` | 300 | 7,420,656 | 6,358,272 | 45,291 | 21,328 | 59:27 | [`05-defect/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/05-defect) |
| 06 | Fool | `M17QSY8K2SDX` | Loss | 48 | 0 | `ENCOUNTER.TEST_SUBJECT_BOSS` | `52f2244` | 185 | 4,062,189 | 3,324,288 | 38,116 | 22,025 | 54:46 | [`06-fool/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/06-fool) |
| 07 | Ironclad | `6AJXKED2SGB1` | Win | 49 | 16 | — | `df68e80` | 299 | 8,049,265 | 7,091,456 | 50,060 | 26,089 | 66:06 | [`07-ironclad/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/07-ironclad) |
| 08 | Silent | `NXZJDJWVP5QE` | Loss | 48 | 0 | `ENCOUNTER.TEST_SUBJECT_BOSS` | `4302239` | 218 | 4,213,654 | 3,497,472 | 33,150 | 14,888 | 49:57 | [`08-silent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/08-silent) |
| 09 | Regent | `HS2JULYVMEG1` | Win | 49 | 57 | — | `4302239` | 260 | 6,353,439 | 5,287,296 | 40,722 | 19,473 | 49:16 | [`09-regent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/09-regent) |
| 10 | Necrobinder | `QN3PMXA654MP` | Win | 49 | 82 | — | `4302239` | 301 | 7,751,666 | 6,702,848 | 49,445 | 24,462 | 61:19 | [`10-necrobinder/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/10-necrobinder) |
| 11 | Defect | `YG8A4U2Q4NNS` | Win | 49 | 29 | — | `80312b7` | 262 | 7,522,241 | 6,594,432 | 37,522 | 17,111 | 45:20 | [`11-defect/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/11-defect) |
| 12 | Fool | `DQPRV7NG001J` | Loss | 49 | 0 | `ENCOUNTER.AEONGLASS_BOSS` | `44cd9b1` | 218 | 6,121,705 | 5,268,992 | 41,166 | 22,553 | 42:01 | [`12-fool/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/12-fool) |
| 13 | Ironclad | `R7X5E4YC3THJ` | Loss | 49 | 0 | `ENCOUNTER.QUEEN_BOSS` | `44cd9b1` | 250 | 5,614,145 | 4,885,760 | 36,875 | 17,424 | 39:42 | [`13-ironclad/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/13-ironclad) |
| 14 | Silent | `LZY086GL2EY8` | Loss | 49 | 0 | `ENCOUNTER.AEONGLASS_BOSS` | `44cd9b1` | 266 | 5,735,320 | 4,908,288 | 45,145 | 22,806 | 47:28 | [`14-silent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/14-silent) |
| 15 | Regent | `LBAJVWWL7RMR` | Loss | 49 | 0 | `ENCOUNTER.TEST_SUBJECT_BOSS` | `44cd9b1` | 245 | 5,906,045 | 5,128,960 | 44,774 | 25,055 | 41:00 | [`15-regent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/15-regent) |
| 16 | Necrobinder | `0XWS2DXRE9PA` | Loss | 27 | 0 | `ENCOUNTER.INFESTED_PRISMS_ELITE` | `44cd9b1` | 128 | 2,476,530 | 2,063,616 | 23,694 | 12,920 | 21:57 | [`16-necrobinder/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/16-necrobinder) |
| 17 | Defect | `0XPJ29PP8VZW` | Loss | 49 | 0 | `ENCOUNTER.AEONGLASS_BOSS` | `44cd9b1` | 302 | 7,964,586 | 7,114,880 | 46,700 | 23,371 | 48:08 | [`17-defect/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/17-defect) |
| 18 | Fool | `DMRBC6B42UUH` | Loss | 48 | 0 | `ENCOUNTER.TEST_SUBJECT_BOSS` | `44cd9b1` | 213 | 4,901,333 | 4,167,296 | 43,450 | 24,907 | 39:23 | [`18-fool/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/18-fool) |
| 19 | Ironclad | `6UKXMSG3B59U` | Loss | 49 | 0 | `ENCOUNTER.AEONGLASS_BOSS` | `44cd9b1` | 290 | 6,522,452 | 5,691,136 | 44,603 | 21,185 | 44:15 | [`19-ironclad/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/19-ironclad) |
| 20 | Silent | `6C38MZWE5Q3A` | Win | 49 | 36 | — | `44cd9b1` | 354 | 9,077,228 | 8,069,888 | 49,511 | 21,242 | 52:06 | [`20-silent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/20-silent) |
| 21 | Regent | `L8ACM2N3EGYE` | Loss | 48 | 0 | `ENCOUNTER.TEST_SUBJECT_BOSS` | `44cd9b1` | 294 | 7,574,300 | 6,627,584 | 49,241 | 26,158 | 50:08 | [`21-regent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/21-regent) |
| 22 | Necrobinder | `1RJRMNDYPPYD` | Win | 49 | 20 | — | `44cd9b1` | 247 | 5,296,374 | 4,448,768 | 49,190 | 27,583 | 48:01 | [`22-necrobinder/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/22-necrobinder) |
| 23 | Defect | `99BRCD6NC1ZY` | Win | 49 | 47 | — | `44cd9b1` | 269 | 6,200,726 | 5,263,488 | 34,856 | 14,394 | 44:23 | [`23-defect/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/23-defect) |
| 24 | Fool | `84QFWS3C2JZ7` | Loss | 17 | 0 | `ENCOUNTER.VANTOM_BOSS` | `44cd9b1` | 80 | 1,350,497 | 1,145,984 | 12,492 | 5,921 | 15:15 | [`24-fool/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/24-fool) |
| 25 | Ironclad | `MSK69DD6QTMU` | Win | 49 | 16 | — | `44cd9b1` | 234 | 5,468,247 | 4,761,088 | 39,701 | 21,404 | 47:13 | [`25-ironclad/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/25-ironclad) |
| 26 | Silent | `DREWDRD62GFQ` | Win | 49 | 1 | — | `44cd9b1` | 307 | 7,881,361 | 6,788,992 | 60,477 | 33,624 | 67:48 | [`26-silent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/26-silent) |
| 27 | Regent | `0XENTSUP0PF4` | Win | 49 | 21 | — | `44cd9b1` | 310 | 7,168,646 | 6,148,096 | 48,956 | 22,822 | 56:18 | [`27-regent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/27-regent) |
| 28 | Necrobinder | `UU1T5RCMHDHN` | Win | 49 | 6 | — | `44cd9b1` | 259 | 5,758,376 | 4,832,256 | 48,386 | 26,740 | 56:22 | [`28-necrobinder/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/28-necrobinder) |
| 29 | Defect | `FN5TECMQG55D` | Win | 49 | 28 | — | `44cd9b1` | 323 | 7,358,598 | 6,341,888 | 43,619 | 18,719 | 68:47 | [`29-defect/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/29-defect) |
| 30 | Fool | `RMHUDHW9035F` | Win | 49 | 14 | — | `44cd9b1` | 222 | 5,205,180 | 4,384,128 | 41,582 | 23,068 | 55:19 | [`30-fool/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/30-fool) |
| 31 | Ironclad | `VNGYNPV5CHT6` | Win | 49 | 30 | — | `44cd9b1` | 244 | 5,945,449 | 5,100,544 | 38,984 | 19,038 | 52:16 | [`31-ironclad/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/31-ironclad) |
| 32 | Silent | `73WL95FHY5F4` | Win | 49 | 45 | — | `44cd9b1` | 320 | 8,864,070 | 7,833,728 | 46,650 | 21,274 | 62:06 | [`32-silent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/32-silent) |
| 33 | Regent | `0NZWMJMEWM3Q` | Win | 49 | 49 | — | `44cd9b1` | 262 | 6,193,740 | 5,256,704 | 42,682 | 20,101 | 52:30 | [`33-regent/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/33-regent) |
| 34 | Necrobinder | `5QZ98LW8H2XJ` | Loss | 33 | 0 | `ENCOUNTER.KAISER_CRAB_BOSS` | `44cd9b1` | 152 | 2,957,773 | 2,471,168 | 27,005 | 14,978 | 30:59 | [`34-necrobinder/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/34-necrobinder) |
| 35 | Defect | `027AG9NV7MMH` | Win | 49 | 51 | — | `44cd9b1` | 257 | 6,384,667 | 5,511,680 | 41,801 | 21,843 | 53:20 | [`35-defect/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/35-defect) |
| 36 | Fool | `1K3J6638M3N6` | Loss | 49 | 0 | `ENCOUNTER.QUEEN_BOSS` | `44cd9b1` | 206 | 4,270,723 | 3,583,232 | 36,630 | 19,078 | 45:07 | [`36-fool/`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10/36-fool) |

### Technical notes

- **Consecutive accounting.** All 36 consecutive rotation runs from start to stop are included; no run was skipped, retried, or excluded.
- **Core revision updates (Runs 01–11).** During the first 11 runs of the live stream, six intermediate merges on the rollover branch were deployed between runs (`16f4447`, `3291601`, `52f2244`, `df68e80`, `4302239`, `80312b7`) before settling on `44cd9b1` for Runs 12–36 (13 wins, 12 losses across those 25 fixed-revision runs).
- **Run 06 (`06-fool`, seed `M17QSY8K2SDX`).** The backend reconnected once mid-run at `node-39` (`cf44acc2a51d4b7e8e6201bfb48cac3b:run-1`), splitting the recording across two SQLite archives (`part1-archive.sqlite` with 131 requests and `part2-archive.sqlite` with 54 requests). The game itself was neither restarted nor reloaded; table totals sum both parts.
- **Profile and recording scope.** Runs executed sequentially on a veteran fully-unlocked Windows profile rather than fresh per-run headless templates, and no local video recording was captured. All 36 runs have both complete core SQLite archives and native `.run` history files.

### Evidence

- **Run records.** Complete SQLite archives, `eval-source.json` provenance files, and native `.run` histories are published in [`sts2-harness-runs/2026-11-26-rotation-a10`](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10).
- **Inspecting an archive.** Run `uv run sts2 journal --archive <run>/archive.sqlite` (or `part1-archive.sqlite` / `part2-archive.sqlite` for Run 06) using core revision `44cd9b1` or later (archive schema 22).
