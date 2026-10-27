import { constants as fsConstants } from "node:fs";
import { lstat, mkdir, open, readdir, realpath, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

export const MAX_STORAGE_STATE_BYTES = 5 * 1024 * 1024;
const MAX_MANIFEST_BYTES = 4 * 1024;
const RETENTION_MS = 24 * 60 * 60 * 1000;
const JOB_PREFIX = "job-";

export class PrivateFileError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function ownerIdentity() {
  if (typeof process.getuid === "function") return `uid:${process.getuid()}`;
  return `user:${os.userInfo().username.toLowerCase()}`;
}

function assertAbsolute(value) {
  if (typeof value !== "string" || !path.isAbsolute(value)) {
    throw new PrivateFileError("invalid_storage_state");
  }
}

function defaultRoot(platform = process.platform, env = process.env, home = os.homedir()) {
  if (platform === "win32") {
    const local = env.LOCALAPPDATA;
    if (!local || !path.isAbsolute(local)) throw new PrivateFileError("private_storage_unavailable");
    return path.join(local, "Titan", "Browser Harness", "artifacts");
  }
  if (platform === "darwin") {
    return path.join(home, "Library", "Application Support", "Titan", "Browser Harness", "artifacts");
  }
  const state = env.XDG_STATE_HOME && path.isAbsolute(env.XDG_STATE_HOME)
    ? env.XDG_STATE_HOME
    : path.join(home, ".local", "state");
  return path.join(state, "titan-browser", "artifacts");
}

function checkPrivateStat(metadata, { directory, requireOwner = true } = {}) {
  if (directory ? !metadata.isDirectory() : !metadata.isFile()) {
    throw new PrivateFileError("private_file_rejected");
  }
  if (process.platform !== "win32") {
    if (requireOwner && metadata.uid !== process.getuid()) throw new PrivateFileError("private_file_rejected");
    if ((metadata.mode & 0o077) !== 0) throw new PrivateFileError("private_file_rejected");
  }
}

export async function readPrivateStorageState(file) {
  try {
    assertAbsolute(file);
    if (process.platform === "win32" || !fsConstants.O_NOFOLLOW) {
      throw new PrivateFileError("secure_storage_state_unsupported");
    }
    const before = await lstat(file);
    if (before.isSymbolicLink()) throw new PrivateFileError("private_file_rejected");
    checkPrivateStat(before, { directory: false });
    if (before.nlink !== 1 || before.size > MAX_STORAGE_STATE_BYTES) {
      throw new PrivateFileError("private_file_rejected");
    }
    const handle = await open(file, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    try {
      const opened = await handle.stat();
      checkPrivateStat(opened, { directory: false });
      if (opened.dev !== before.dev || opened.ino !== before.ino || opened.nlink !== 1) {
        throw new PrivateFileError("private_file_rejected");
      }
      const chunks = [];
      const chunk = Buffer.alloc(64 * 1024);
      let total = 0;
      while (true) {
        const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
        if (bytesRead === 0) break;
        total += bytesRead;
        if (total > MAX_STORAGE_STATE_BYTES) throw new PrivateFileError("private_file_rejected");
        chunks.push(Buffer.from(chunk.subarray(0, bytesRead)));
      }
      const parsed = JSON.parse(Buffer.concat(chunks, total).toString("utf8"));
      if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.cookies) || !Array.isArray(parsed.origins)) {
        throw new PrivateFileError("private_file_rejected");
      }
      return parsed;
    } finally {
      await handle.close();
    }
  } catch (error) {
    if (error instanceof PrivateFileError) throw error;
    throw new PrivateFileError("private_file_rejected");
  }
}

async function assertPrivateDirectory(directory) {
  const metadata = await lstat(directory);
  if (metadata.isSymbolicLink()) throw new PrivateFileError("private_storage_unavailable");
  checkPrivateStat(metadata, { directory: true });
  const resolved = await realpath(directory);
  const resolvedParent = await realpath(path.dirname(directory));
  if (path.dirname(resolved) !== resolvedParent || path.basename(resolved) !== path.basename(directory)) {
    throw new PrivateFileError("private_storage_unavailable");
  }
  return metadata;
}

export async function ensureArtifactRoot(root = defaultRoot(), platform = process.platform) {
  try {
    if (platform === "win32") throw new PrivateFileError("private_storage_unavailable");
    await mkdir(root, { recursive: true, mode: 0o700 });
    const metadata = await assertPrivateDirectory(root);
    return { root, owner: ownerIdentity(), metadata };
  } catch (error) {
    if (error instanceof PrivateFileError) throw error;
    throw new PrivateFileError("private_storage_unavailable");
  }
}

async function writeManifest(directory, manifest, { create = false } = {}) {
  const manifestPath = path.join(directory, "manifest.json");
  const flags = fsConstants.O_WRONLY | fsConstants.O_NOFOLLOW | (create ? fsConstants.O_CREAT | fsConstants.O_EXCL : 0);
  const handle = await open(manifestPath, flags, 0o600);
  try {
    const metadata = await handle.stat();
    checkPrivateStat(metadata, { directory: false });
    if (!create && metadata.nlink !== 1) throw new PrivateFileError("manifest_rejected");
    await handle.truncate(0);
    await handle.writeFile(`${JSON.stringify(manifest)}\n`, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function readManifest(directory) {
  const manifestPath = path.join(directory, "manifest.json");
  const metadata = await lstat(manifestPath);
  if (metadata.isSymbolicLink() || metadata.size > MAX_MANIFEST_BYTES) {
    throw new PrivateFileError("manifest_rejected");
  }
  checkPrivateStat(metadata, { directory: false });
  const handle = await open(manifestPath, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
  try {
    const opened = await handle.stat();
    checkPrivateStat(opened, { directory: false });
    if (opened.dev !== metadata.dev || opened.ino !== metadata.ino || opened.nlink !== 1) {
      throw new PrivateFileError("manifest_rejected");
    }
    const body = await handle.readFile("utf8");
    if (Buffer.byteLength(body) > MAX_MANIFEST_BYTES) throw new PrivateFileError("manifest_rejected");
    return JSON.parse(body);
  } finally {
    await handle.close();
  }
}

export async function createManagedJobDir({ jobId, root = defaultRoot() }) {
  const { root: safeRoot, owner } = await ensureArtifactRoot(root);
  const id = randomUUID();
  const directory = path.join(safeRoot, `${JOB_PREFIX}${id}`);
  try {
    await mkdir(directory, { mode: 0o700 });
    await assertPrivateDirectory(directory);
    const now = Date.now();
    await writeManifest(directory, {
      schema: 1,
      owner,
      jobId,
      status: "active",
      createdAt: now,
      expiresAt: now + RETENTION_MS,
    }, { create: true });
    return { directory, root: safeRoot };
  } catch (error) {
    try { await rm(directory, { recursive: true, force: true }); }
    catch { throw new PrivateFileError("private_storage_cleanup_failed"); }
    if (error instanceof PrivateFileError) throw error;
    throw new PrivateFileError("private_storage_unavailable");
  }
}

export async function markJobComplete(directory, status) {
  if (!['ok', 'failed'].includes(status)) throw new PrivateFileError("manifest_rejected");
  try {
    await assertPrivateDirectory(directory);
    const manifest = await readManifest(directory);
    if (manifest.owner !== ownerIdentity() || manifest.status !== "active") {
      throw new PrivateFileError("manifest_rejected");
    }
    await writeManifest(directory, { ...manifest, status: "complete", outcome: status, completedAt: Date.now() });
  } catch (error) {
    if (error instanceof PrivateFileError) throw error;
    throw new PrivateFileError("manifest_rejected");
  }
}

export async function removeManagedJobDir(directory, root = defaultRoot()) {
  const { root: safeRoot } = await ensureArtifactRoot(root);
  const metadata = await assertPrivateDirectory(directory);
  const parent = path.dirname(directory);
  if (parent !== safeRoot || metadata.uid !== (process.getuid?.() ?? metadata.uid)) {
    throw new PrivateFileError("manifest_rejected");
  }
  const manifest = await readManifest(directory);
  if (manifest.owner !== ownerIdentity() || manifest.status !== "complete") {
    throw new PrivateFileError("manifest_rejected");
  }
  await rm(directory, { recursive: true, force: false });
}

function validManifest(manifest, owner, now) {
  return manifest && manifest.schema === 1 && manifest.owner === owner
    && manifest.status === "complete" && ["ok", "failed"].includes(manifest.outcome)
    && Number.isSafeInteger(manifest.createdAt)
    && Number.isSafeInteger(manifest.expiresAt) && manifest.expiresAt - manifest.createdAt === RETENTION_MS
    && manifest.expiresAt <= now;
}

export async function cleanupExpiredArtifacts({ root = defaultRoot(), now = Date.now() } = {}) {
  const { root: safeRoot, owner } = await ensureArtifactRoot(root);
  const report = { removed: 0, preserved: 0, failed: 0 };
  const entries = await readdir(safeRoot, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.name.startsWith(JOB_PREFIX) || entry.isSymbolicLink() || !entry.isDirectory()) {
      report.preserved += 1;
      continue;
    }
    const directory = path.join(safeRoot, entry.name);
    try {
      const metadata = await assertPrivateDirectory(directory);
      if (process.platform !== "win32" && metadata.uid !== process.getuid()) {
        report.preserved += 1;
        continue;
      }
      const manifest = await readManifest(directory);
      if (!validManifest(manifest, owner, now)) {
        report.preserved += 1;
        continue;
      }
      await rm(directory, { recursive: true, force: false });
      report.removed += 1;
    } catch {
      report.failed += 1;
    }
  }
  return report;
}

export async function main(argv = process.argv.slice(2)) {
  if (argv.length !== 1 || argv[0] !== "cleanup") {
    process.stderr.write("Usage: private-files.js cleanup\n");
    return 2;
  }
  try {
    const result = await cleanupExpiredArtifacts();
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return result.failed ? 1 : 0;
  } catch {
    process.stdout.write('{"status":"cleanup_failed"}\n');
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().then((code) => { process.exitCode = code; });
}
