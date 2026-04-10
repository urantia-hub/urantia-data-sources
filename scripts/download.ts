/**
 * Download data from the R2 bucket to local data/ directory.
 *
 * Usage:
 *   bun run download                    # download everything
 *   bun run download json               # download only json/
 *   bun run download entities           # download only entities/
 *   bun run download audio/eng/papers   # download a specific subdirectory
 */
import { GetObjectCommand, ListObjectsV2Command } from "@aws-sdk/client-s3";
import { mkdir, stat, writeFile } from "fs/promises";
import { join, dirname } from "path";
import { r2, BUCKET_NAME } from "./r2-config";

const DATA_DIR = join(import.meta.dir, "../data");
const prefix = process.argv[2] ?? "";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)}GB`;
}

async function listAllKeys(prefix: string): Promise<{ key: string; size: number }[]> {
  const keys: { key: string; size: number }[] = [];
  let continuationToken: string | undefined;

  do {
    const response = await r2.send(
      new ListObjectsV2Command({
        Bucket: BUCKET_NAME,
        Prefix: prefix || undefined,
        ContinuationToken: continuationToken,
      })
    );

    for (const obj of response.Contents ?? []) {
      if (obj.Key && obj.Size !== undefined) {
        keys.push({ key: obj.Key, size: obj.Size });
      }
    }

    continuationToken = response.IsTruncated
      ? response.NextContinuationToken
      : undefined;
  } while (continuationToken);

  return keys;
}

async function downloadFile(key: string): Promise<number> {
  const localPath = join(DATA_DIR, key);
  await mkdir(dirname(localPath), { recursive: true });

  const response = await r2.send(
    new GetObjectCommand({
      Bucket: BUCKET_NAME,
      Key: key,
    })
  );

  if (!response.Body) {
    throw new Error(`Empty response for ${key}`);
  }

  const bytes = await response.Body.transformToByteArray();
  await writeFile(localPath, bytes);
  return bytes.length;
}

async function main() {
  console.log(`Downloading from R2 bucket '${BUCKET_NAME}' (prefix: '${prefix || "(all)"}')...`);

  const objects = await listAllKeys(prefix);
  if (objects.length === 0) {
    console.log("No objects found matching the prefix.");
    return;
  }

  console.log(`Found ${objects.length} files to download (${formatSize(objects.reduce((s, o) => s + o.size, 0))} total)`);
  let downloaded = 0;
  let totalSize = 0;

  for (const obj of objects) {
    // Skip if local file exists and is the same size
    const localPath = join(DATA_DIR, obj.key);
    try {
      const localStat = await stat(localPath);
      if (localStat.size === obj.size) {
        downloaded++;
        totalSize += obj.size;
        continue; // Already downloaded
      }
    } catch {
      // File doesn't exist locally, download it
    }

    const size = await downloadFile(obj.key);
    downloaded++;
    totalSize += size;

    if (downloaded % 100 === 0 || downloaded === objects.length) {
      console.log(`  ${downloaded}/${objects.length} files (${formatSize(totalSize)})`);
    }
  }

  console.log(`\nDone: ${downloaded} files (${formatSize(totalSize)} total)`);
}

main().catch(console.error);
