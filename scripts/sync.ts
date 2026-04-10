/**
 * Compare local data/ directory with R2 bucket contents.
 * Shows files that are missing, extra, or different sizes.
 *
 * Usage:
 *   bun run sync              # dry-run comparison
 *   bun run sync --upload     # upload missing/changed files to R2
 */
import { ListObjectsV2Command, PutObjectCommand } from "@aws-sdk/client-s3";
import { Upload } from "@aws-sdk/lib-storage";
import { readdir, stat } from "fs/promises";
import { join, relative } from "path";
import { createReadStream } from "fs";
import { r2, BUCKET_NAME } from "./r2-config";

const DATA_DIR = join(import.meta.dir, "../data");
const shouldUpload = process.argv.includes("--upload");

const MIME_TYPES: Record<string, string> = {
  ".json": "application/json",
  ".mp3": "audio/mpeg",
  ".txt": "text/plain",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)}GB`;
}

async function* walkDir(dir: string): AsyncGenerator<{ key: string; size: number }> {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walkDir(fullPath);
    } else {
      const s = await stat(fullPath);
      yield { key: relative(DATA_DIR, fullPath), size: s.size };
    }
  }
}

async function listR2Objects(): Promise<Map<string, number>> {
  const objects = new Map<string, number>();
  let continuationToken: string | undefined;

  do {
    const response = await r2.send(
      new ListObjectsV2Command({
        Bucket: BUCKET_NAME,
        ContinuationToken: continuationToken,
      })
    );

    for (const obj of response.Contents ?? []) {
      if (obj.Key && obj.Size !== undefined) {
        objects.set(obj.Key, obj.Size);
      }
    }

    continuationToken = response.IsTruncated
      ? response.NextContinuationToken
      : undefined;
  } while (continuationToken);

  return objects;
}

async function uploadFile(filePath: string, key: string) {
  const ext = filePath.substring(filePath.lastIndexOf("."));
  const contentType = MIME_TYPES[ext] ?? "application/octet-stream";
  const fileSize = (await stat(filePath)).size;

  if (fileSize > 10 * 1024 * 1024) {
    const upload = new Upload({
      client: r2,
      params: {
        Bucket: BUCKET_NAME,
        Key: key,
        Body: createReadStream(filePath),
        ContentType: contentType,
      },
      partSize: 10 * 1024 * 1024,
      leavePartsOnError: false,
    });
    await upload.done();
  } else {
    const body = await Bun.file(filePath).arrayBuffer();
    await r2.send(
      new PutObjectCommand({
        Bucket: BUCKET_NAME,
        Key: key,
        Body: new Uint8Array(body),
        ContentType: contentType,
      })
    );
  }
  console.log(`  Uploaded ${key} (${formatSize(fileSize)})`);
}

async function main() {
  console.log("Comparing local data/ with R2 bucket...\n");

  const r2Objects = await listR2Objects();
  const localFiles = new Map<string, number>();

  for await (const file of walkDir(DATA_DIR)) {
    localFiles.set(file.key, file.size);
  }

  const onlyLocal: string[] = [];
  const onlyR2: string[] = [];
  const sizeMismatch: string[] = [];
  const matching: string[] = [];

  for (const [key, size] of localFiles) {
    const r2Size = r2Objects.get(key);
    if (r2Size === undefined) {
      onlyLocal.push(key);
    } else if (r2Size !== size) {
      sizeMismatch.push(key);
    } else {
      matching.push(key);
    }
  }

  for (const key of r2Objects.keys()) {
    if (!localFiles.has(key)) {
      onlyR2.push(key);
    }
  }

  console.log(`Matching:        ${matching.length} files`);
  console.log(`Only local:      ${onlyLocal.length} files`);
  console.log(`Only R2:         ${onlyR2.length} files`);
  console.log(`Size mismatch:   ${sizeMismatch.length} files`);

  if (onlyLocal.length > 0) {
    console.log("\n--- Only in local (not in R2) ---");
    for (const key of onlyLocal.slice(0, 20)) {
      console.log(`  + ${key} (${formatSize(localFiles.get(key)!)})`);
    }
    if (onlyLocal.length > 20) console.log(`  ... and ${onlyLocal.length - 20} more`);
  }

  if (onlyR2.length > 0) {
    console.log("\n--- Only in R2 (not local) ---");
    for (const key of onlyR2.slice(0, 20)) {
      console.log(`  - ${key} (${formatSize(r2Objects.get(key)!)})`);
    }
    if (onlyR2.length > 20) console.log(`  ... and ${onlyR2.length - 20} more`);
  }

  if (sizeMismatch.length > 0) {
    console.log("\n--- Size mismatch ---");
    for (const key of sizeMismatch.slice(0, 20)) {
      console.log(`  ~ ${key} (local: ${formatSize(localFiles.get(key)!)}, R2: ${formatSize(r2Objects.get(key)!)})`);
    }
  }

  if (shouldUpload && (onlyLocal.length > 0 || sizeMismatch.length > 0)) {
    console.log("\nUploading missing/changed files to R2...");
    const toUpload = [...onlyLocal, ...sizeMismatch];
    for (const key of toUpload) {
      await uploadFile(join(DATA_DIR, key), key);
    }
    console.log(`\nUploaded ${toUpload.length} files.`);
  } else if (onlyLocal.length > 0 || sizeMismatch.length > 0) {
    console.log("\nRun with --upload to push local changes to R2");
  }
}

main().catch(console.error);
