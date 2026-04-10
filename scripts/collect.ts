/**
 * Collect and deduplicate all data from source repos into data/.
 * For duplicate audio files, keeps the most recently modified version.
 *
 * This is a one-time operation to consolidate data before uploading to R2.
 *
 * Usage:
 *   bun run collect           # collect all data
 *   bun run collect --dry-run # show what would be collected without copying
 */
import { readdir, stat, mkdir, copyFile } from "fs/promises";
import { join, basename, resolve } from "path";

const ROOT = resolve(import.meta.dir, "../../");
const DATA_DIR = join(import.meta.dir, "../data");
const DRY_RUN = process.argv.includes("--dry-run");

// Source directories
const SOURCES = {
  papersJson: join(ROOT, "urantia-papers-json/data"),
  hubApi: join(ROOT, "urantia-hub-api/public/data"),
  originalAudio: join(ROOT, "original_audio_ub"),
  devApiData: join(ROOT, "urantia-dev-api/data"),
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)}GB`;
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function* walkDir(dir: string): AsyncGenerator<string> {
  if (!(await exists(dir))) return;
  const entries = await readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walkDir(fullPath);
    } else {
      yield fullPath;
    }
  }
}

async function copyToData(src: string, destKey: string) {
  const dest = join(DATA_DIR, destKey);
  if (DRY_RUN) {
    const s = await stat(src);
    console.log(`  [dry-run] ${destKey} (${formatSize(s.size)})`);
    return;
  }
  await mkdir(join(dest, ".."), { recursive: true });
  await copyFile(src, dest);
}

/** Given multiple source paths for the same logical file, return the most recently modified */
async function newestFile(paths: string[]): Promise<string | null> {
  let newest: string | null = null;
  let newestMtime = 0;

  for (const p of paths) {
    try {
      const s = await stat(p);
      if (s.mtimeMs > newestMtime) {
        newestMtime = s.mtimeMs;
        newest = p;
      }
    } catch {
      // File doesn't exist at this source
    }
  }
  return newest;
}

/** Classify an audio file into the target R2 key structure */
function classifyAudioFile(name: string, relPath?: string): string {
  // Part-level files
  if (name.match(/^tts-1(-hd)?-\w+-Part\d/)) {
    return `audio/eng/parts/${name}`;
  }
  // Audiobook files
  if (name.startsWith("audiobook-")) {
    return `audio/eng/audiobook/${name}`;
  }
  // Whole-paper files (0.mp3 - 196.mp3)
  if (name.match(/^\d+\.mp3$/)) {
    return `audio/eng/papers/${name}`;
  }
  // Files in papers/ subdirectory
  if (relPath?.startsWith("papers/")) {
    return `audio/eng/papers/${name}`;
  }
  // Files in audiobook/ subdirectory
  if (relPath?.startsWith("audiobook/")) {
    return `audio/eng/audiobook/${relPath}`;
  }
  // Voice-based paragraph files
  const voiceMatch = name.match(/^tts-1(-hd)?-(nova|echo|onyx|shimmer|fable|alloy)-/);
  if (voiceMatch) {
    return `audio/eng/paragraphs/${voiceMatch[2]}/${name}`;
  }
  return `audio/eng/other/${name}`;
}

// ─── JSON Data ───────────────────────────────────────────────────────────

async function collectJson() {
  const jsonDir = join(SOURCES.papersJson, "json/eng");
  if (!(await exists(jsonDir))) {
    console.log("  SKIP: urantia-papers-json/data/json/eng not found");
    return;
  }

  console.log("\n[json] Collecting papers JSON...");
  let count = 0;
  for await (const filePath of walkDir(jsonDir)) {
    if (!filePath.endsWith(".json")) continue;
    const relPath = filePath.substring(jsonDir.length + 1);
    await copyToData(filePath, `json/eng/${relPath}`);
    count++;
  }
  console.log(`  Collected ${count} JSON files`);
}

// ─── Audio Data ──────────────────────────────────────────────────────────

async function collectAudio() {
  console.log("\n[audio] Collecting and deduplicating audio files...");

  const audioSources = new Map<string, string[]>();

  function addSource(key: string, sourcePath: string) {
    const existing = audioSources.get(key) ?? [];
    existing.push(sourcePath);
    audioSources.set(key, existing);
  }

  async function scanDir(dir: string, label: string, baseDir?: string) {
    if (!(await exists(dir))) return;
    console.log(`  Scanning ${label}...`);
    for await (const filePath of walkDir(dir)) {
      if (!filePath.endsWith(".mp3")) continue;
      const name = basename(filePath);
      const relPath = baseDir ? filePath.substring(baseDir.length + 1) : undefined;
      addSource(classifyAudioFile(name, relPath), filePath);
    }
  }

  const ppjAudioDir = join(SOURCES.papersJson, "mp3/eng");
  const hubAudioDir = join(SOURCES.hubApi, "mp3/eng");

  await scanDir(ppjAudioDir, "urantia-papers-json/data/mp3/eng", ppjAudioDir);
  await scanDir(SOURCES.originalAudio, "original_audio_ub");
  await scanDir(hubAudioDir, "urantia-hub-api/public/data/mp3/eng", hubAudioDir);

  // Deduplicate: for each target key, pick the newest source
  let count = 0;
  let dupes = 0;
  for (const [key, sources] of audioSources) {
    if (sources.length > 1) dupes++;
    const winner = await newestFile(sources);
    if (winner) {
      await copyToData(winner, key);
      count++;
    }
  }
  console.log(`  Collected ${count} audio files (${dupes} duplicates resolved by keeping newest)`);
}

// ─── Entity Data ─────────────────────────────────────────────────────────

async function collectEntities() {
  console.log("\n[entities] Collecting entity data...");
  const entitiesDir = join(SOURCES.devApiData, "entities");

  const seedEntities = join(entitiesDir, "seed-entities.json");
  if (await exists(seedEntities)) {
    await copyToData(seedEntities, "entities/seed-entities.json");
    console.log("  Collected seed-entities.json");
  }

  const paragraphEntities = join(entitiesDir, "paragraph-entities.json");
  if (await exists(paragraphEntities)) {
    await copyToData(paragraphEntities, "entities/paragraph-entities.json");
    console.log("  Collected paragraph-entities.json");
  }
}

// ─── Audio Manifest ──────────────────────────────────────────────────────

async function collectManifest() {
  console.log("\n[manifests] Collecting audio manifest...");
  const manifest = join(SOURCES.devApiData, "audio-manifest.json");
  if (await exists(manifest)) {
    await copyToData(manifest, "manifests/audio-manifest.json");
    console.log("  Collected audio-manifest.json");
  }
}

// ─── Embeddings ──────────────────────────────────────────────────────────

async function collectEmbeddings() {
  console.log("\n[embeddings] Collecting embeddings...");
  const embeddings = join(SOURCES.devApiData, "embeddings.json");
  if (await exists(embeddings)) {
    await copyToData(embeddings, "embeddings/embeddings.json");
    const s = await stat(embeddings);
    console.log(`  Collected embeddings.json (${formatSize(s.size)})`);
  } else {
    console.log("  SKIP: embeddings.json not found (455MB, may be gitignored)");
  }
}

// ─── Main ────────────────────────────────────────────────────────────────

async function main() {
  console.log("Collecting data from all source repos into data/");
  if (DRY_RUN) console.log("(DRY RUN — no files will be copied)\n");

  // Check which sources exist
  for (const [name, path] of Object.entries(SOURCES)) {
    const found = await exists(path);
    console.log(`  ${found ? "OK" : "MISSING"}: ${name} → ${path}`);
  }

  if (!DRY_RUN) {
    await mkdir(DATA_DIR, { recursive: true });
  }

  await collectJson();
  await collectAudio();
  await collectEntities();
  await collectManifest();
  await collectEmbeddings();

  console.log("\nCollection complete! Next steps:");
  console.log("  1. Review data/ directory structure");
  console.log("  2. Run: bun run upload");
}

main().catch(console.error);
