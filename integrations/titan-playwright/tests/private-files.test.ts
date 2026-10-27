import { chmod, lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";
import {
  cleanupExpiredArtifacts,
  createManagedJobDir,
  ensureArtifactRoot,
  markJobComplete,
  MAX_STORAGE_STATE_BYTES,
  readPrivateStorageState,
} from "../src/private-files.js";

const roots: string[] = [];

async function tempRoot() {
  const root = await mkdtemp(path.join(tmpdir(), "titan-private-files-test-"));
  roots.push(root);
  return root;
}

test.afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function privateState(directory: string) {
  const file = path.join(directory, "state.json");
  await writeFile(file, JSON.stringify({ cookies: [], origins: [] }), { mode: 0o600 });
  await chmod(file, 0o600);
  return file;
}

test("private storage state requires an absolute private regular file and does not change it", async () => {
  const directory = await tempRoot();
  const file = await privateState(directory);
  const before = await lstat(file);
  expect(await readPrivateStorageState(file)).toEqual({ cookies: [], origins: [] });
  const after = await lstat(file);
  expect(after.mtimeMs).toBe(before.mtimeMs);
  await expect(readPrivateStorageState(path.relative(process.cwd(), file))).rejects.toMatchObject({
    code: "invalid_storage_state",
  });
});

test("storage-state symlinks and group-readable files are rejected", async () => {
  const directory = await tempRoot();
  const file = await privateState(directory);
  const link = path.join(directory, "linked-state.json");
  await symlink(file, link);
  await expect(readPrivateStorageState(link)).rejects.toMatchObject({ code: "private_file_rejected" });
  await chmod(file, 0o640);
  await expect(readPrivateStorageState(file)).rejects.toMatchObject({ code: "private_file_rejected" });
});

test("storage-state files owned by another uid are rejected", async () => {
  test.skip(typeof process.getuid !== "function", "uid ownership is not exposed on Windows");
  const directory = await tempRoot();
  const file = await privateState(directory);
  const getuid = process.getuid;
  process.getuid = () => getuid() + 1;
  try {
    await expect(readPrivateStorageState(file)).rejects.toMatchObject({ code: "private_file_rejected" });
  } finally {
    process.getuid = getuid;
  }
});

test("oversized storage-state files are rejected before unbounded reads", async () => {
  const directory = await tempRoot();
  const file = path.join(directory, "large-state.json");
  await writeFile(file, Buffer.alloc(MAX_STORAGE_STATE_BYTES + 1), { mode: 0o600 });
  await expect(readPrivateStorageState(file)).rejects.toMatchObject({ code: "private_file_rejected" });
});

test("artifact root rejects permissive permissions without repairing it", async () => {
  test.skip(process.platform === "win32", "POSIX mode bits are not available on Windows");
  const root = await tempRoot();
  await chmod(root, 0o755);
  await expect(ensureArtifactRoot(root)).rejects.toMatchObject({ code: "private_file_rejected" });
  expect((await lstat(root)).mode & 0o777).toBe(0o755);
});

test("Windows artifact storage fails closed until private ACL checks are supported", async () => {
  const root = path.join(tmpdir(), `titan-windows-artifacts-${randomUUID()}`);
  await expect(ensureArtifactRoot(root, "win32")).rejects.toMatchObject({ code: "private_storage_unavailable" });
  await expect(lstat(root)).rejects.toMatchObject({ code: "ENOENT" });
});

test("cleanup removes only expired completed owned job directories", async () => {
  test.skip(process.platform === "win32", "manifest mode validation requires POSIX permissions");
  const root = await tempRoot();
  const active = await createManagedJobDir({ jobId: "active", root });
  const expired = await createManagedJobDir({ jobId: "expired", root });
  await markJobComplete(expired.directory, "failed");
  const manifestPath = path.join(expired.directory, "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  manifest.createdAt -= 48 * 60 * 60 * 1000;
  manifest.expiresAt -= 48 * 60 * 60 * 1000;
  await writeFile(manifestPath, `${JSON.stringify(manifest)}\n`, { mode: 0o600 });
  await chmod(manifestPath, 0o600);
  const unknown = path.join(root, "job-unrecognized");
  await mkdir(unknown, { mode: 0o700 });
  const link = path.join(root, "job-linked");
  await symlink(expired.directory, link);

  const report = await cleanupExpiredArtifacts({ root });

  expect(report.removed).toBe(1);
  expect(report.preserved).toBeGreaterThanOrEqual(2);
  expect(await lstat(active.directory)).toBeTruthy();
  expect(await lstat(unknown)).toBeTruthy();
  expect((await lstat(link)).isSymbolicLink()).toBe(true);
  await expect(lstat(expired.directory)).rejects.toMatchObject({ code: "ENOENT" });
});
