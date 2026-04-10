# urantia-data-sources

Single source of truth for all Urantia project data. All data is stored in a Cloudflare R2 bucket — this repo contains only scripts for managing it.

## R2 Bucket Structure

```
urantia-data-sources/
├── json/eng/                        Papers, sections, paragraphs (197 files + index)
│   ├── 000.json - 196.json
│   ├── 0-foreword.json
│   ├── 1-part.json - 4-part.json
│   └── index.json
├── audio/eng/
│   ├── paragraphs/                  Paragraph-level TTS audio by voice
│   │   ├── nova/                    ~16,200 files (primary voice)
│   │   ├── echo/
│   │   ├── onyx/
│   │   ├── shimmer/
│   │   ├── fable/
│   │   └── alloy/
│   ├── papers/                      197 whole-paper MP3s (0.mp3 - 196.mp3)
│   ├── parts/                       Part-level audio files
│   └── audiobook/                   Audiobook processing files (intro, ending, background)
│       └── papers/                  Named paper audiobook files
├── entities/
│   ├── seed-entities.json           ~3,000+ entities (beings, places, concepts, etc.)
│   └── paragraph-entities.json      Citation-to-entity mappings
├── embeddings/
│   └── embeddings.json              OpenAI text-embedding-3-small vectors (455MB)
└── manifests/
    └── audio-manifest.json          Audio file metadata for API seeding
```

## Setup

```bash
bun install
cp .env.example .env
# Fill in R2 credentials (see .env.example for instructions)
```

## Scripts

| Command | Description |
|---------|-------------|
| `bun run collect` | Gather data from all source repos into local `data/` directory |
| `bun run collect:dry-run` | Preview what would be collected without copying |
| `bun run upload` | Upload local `data/` to R2 |
| `bun run upload json` | Upload only a specific subdirectory |
| `bun run download` | Download all data from R2 to local `data/` |
| `bun run download json` | Download only a specific subdirectory |
| `bun run sync` | Compare local `data/` with R2 (dry-run) |
| `bun run sync --upload` | Sync differences to R2 |

## Initial Population

One-time steps to populate R2 from existing source repos:

```bash
# 1. Collect and deduplicate data from all source repos
bun run collect

# 2. Review what was collected
ls -la data/

# 3. Upload to R2
bun run upload
```

The `collect` script pulls from these source directories (expected as siblings):
- `../urantia-papers-json/data/` — papers JSON + paragraph audio
- `../urantia-hub-api/public/data/` — whole-paper audio + audiobook files
- `../original_audio_ub/` — paragraph audio (multiple voices)
- `../urantia-dev-api/data/` — entities, audio manifest, embeddings

For duplicate audio files (same filename across repos), the most recently modified version is kept.

## Data Lineage

| Data | Original Source | How Generated |
|------|----------------|---------------|
| Papers JSON | Urantia Book text | Structured by urantia-papers-json project |
| Paragraph audio | OpenAI TTS API | `tts-1-hd` model, multiple voices |
| Paper audio | Concatenated paragraph audio | Combined per-paper |
| Entities | [Urantiapedia](https://github.com/JanHerca/urantiapedia) topic index | `urantia-dev-api/scripts/entities/parse-topic-index.ts` |
| Embeddings | OpenAI Embeddings API | `urantia-dev-api/scripts/generate-embeddings.ts` |
| Audio manifest | Audio file inventory | `urantia-dev-api/scripts/generate-audio-manifest.ts` |

## Redundancy

The R2 bucket provides **11 nines (99.999999999%) durability**. As additional safety nets:
- The original source repos continue to exist as de facto backups
- Recommended: quarterly `rclone sync` to a local external drive

## For urantia-dev-api Maintainers

See `DATA_PIPELINE.md` in the urantia-dev-api repo for how to download data from R2 and seed the database.

## License

This project is licensed under the [MIT License](./LICENSE).

## Disclaimer

This is an independent community project by [Adams Technologies LLC](https://adamstechnologies.com). It is not affiliated with, endorsed by, or connected with Urantia Foundation. The original English text of *The Urantia Book* is in the public domain (*Michael Foundation v. Urantia Foundation*, 10th Cir. 2003). All use of "Urantia" is nominative fair use to identify the subject matter.
