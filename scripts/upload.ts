/**
 * Upload local data files to the R2 bucket.
 *
 * Usage:
 *   bun run upload                    # upload everything in data/
 *   bun run upload json               # upload only json/
 *   bun run upload audio/eng/papers   # upload a specific subdirectory
 */
import { PutObjectCommand } from "@aws-sdk/client-s3";
import { Upload } from "@aws-sdk/lib-storage";
import { readdir, stat } from "fs/promises";
import { join, relative } from "path";
import { createReadStream } from "fs";
import { r2, BUCKET_NAME } from "./r2-config";

const DATA_DIR = join(import.meta.dir, "../data");
const prefix = process.argv[2] ?? "";
const uploadRoot = join(DATA_DIR, prefix);

const MIME_TYPES: Record<string, string> = {
  ".json": "application/json",
  ".mp3": "audio/mpeg",
  ".txt": "text/plain",
};

async function* walkDir(dir: string): AsyncGenerator<string> {
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

async function uploadFile(filePath: string) {
  const key = relative(DATA_DIR, filePath);
  const ext = filePath.substring(filePath.lastIndexOf("."));
  const contentType = MIME_TYPES[ext] ?? "application/octet-stream";
  const fileSize = (await stat(filePath)).size;

  // Use multipart upload for files > 10MB
  if (fileSize > 10 * 1024 * 1024) {
    const upload = new Upload({
      client: r2,
      params: {
        Bucket: BUCKET_NAME,
        Key: key,
        Body: createReadStream(filePath),
        ContentType: contentType,
      },
      partSize: 10 * 1024 * 1024, // 10MB parts
      leavePartsOnError: false,
    });

    upload.on("httpUploadProgress", (progress) => {
      const pct = progress.loaded && progress.total
        ? Math.round((progress.loaded / progress.total) * 100)
        : "?";
      process.stdout.write(`\r  Uploading ${key}: ${pct}%`);
    });

    await upload.done();
    console.log(`\r  Uploaded ${key} (${formatSize(fileSize)})`);
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
    console.log(`  Uploaded ${key} (${formatSize(fileSize)})`);
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)}GB`;
}

async function main() {
  try {
    await stat(uploadRoot);
  } catch {
    console.error(`Directory not found: ${uploadRoot}`);
    console.error("Run the collect script first to populate data/");
    process.exit(1);
  }

  console.log(`Uploading from ${uploadRoot} to R2 bucket '${BUCKET_NAME}'...`);
  let count = 0;
  let totalSize = 0;

  for await (const filePath of walkDir(uploadRoot)) {
    const fileSize = (await stat(filePath)).size;
    await uploadFile(filePath);
    count++;
    totalSize += fileSize;
  }

  console.log(`\nDone: ${count} files uploaded (${formatSize(totalSize)} total)`);
}

main().catch(console.error);
