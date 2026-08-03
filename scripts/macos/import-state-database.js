#!/usr/bin/env node

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const [encryptedPath, privateKeyPath, projectRootInput] = process.argv.slice(2);
if (!encryptedPath || !privateKeyPath || !projectRootInput) {
  console.error(
    "Usage: node import-state-database.js <encrypted-database> " +
      "<private-key> <project-root>",
  );
  process.exit(2);
}

const projectRoot = path.resolve(projectRootInput);
if (
  !fs.existsSync(path.join(projectRoot, "app", "dingtalk_stream_agent.py")) ||
  !fs.existsSync(path.join(projectRoot, ".env"))
) {
  throw new Error("project root is invalid or not configured");
}

let envelope;
try {
  envelope = JSON.parse(fs.readFileSync(encryptedPath, "utf8"));
} catch (_) {
  throw new Error("encrypted database is missing or invalid");
}
if (
  envelope.version !== 1 ||
  envelope.purpose !== "dingtalk-state-database" ||
  envelope.key_algorithm !== "RSA-OAEP-SHA256" ||
  envelope.content_algorithm !== "AES-256-GCM" ||
  !/^[0-9a-f]{64}$/.test(envelope.plaintext_sha256 || "")
) {
  throw new Error("encrypted database has an unsupported format");
}

let plaintext;
let aesKey;
try {
  const privateKey = fs.readFileSync(privateKeyPath, "utf8");
  aesKey = crypto.privateDecrypt(
    {
      key: privateKey,
      padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
      oaepHash: "sha256",
    },
    Buffer.from(envelope.encrypted_key, "base64"),
  );
  const decipher = crypto.createDecipheriv(
    "aes-256-gcm",
    aesKey,
    Buffer.from(envelope.nonce, "base64"),
  );
  decipher.setAuthTag(Buffer.from(envelope.tag, "base64"));
  plaintext = Buffer.concat([
    decipher.update(Buffer.from(envelope.ciphertext, "base64")),
    decipher.final(),
  ]);
} catch (_) {
  throw new Error("encrypted database could not be authenticated or decrypted");
} finally {
  if (aesKey) {
    aesKey.fill(0);
  }
}

if (
  plaintext.length < 100 ||
  plaintext.subarray(0, 16).toString("binary") !== "SQLite format 3\u0000"
) {
  plaintext.fill(0);
  throw new Error("decrypted content is not a SQLite database");
}
const actualHash = crypto.createHash("sha256").update(plaintext).digest("hex");
if (actualHash !== envelope.plaintext_sha256) {
  plaintext.fill(0);
  throw new Error("decrypted database hash does not match");
}

const runtimeRoot = path.join(projectRoot, ".runtime", "customer-service");
const targetPath = path.join(runtimeRoot, "dingtalk.db");
const walPath = `${targetPath}-wal`;
const shmPath = `${targetPath}-shm`;
if (fs.existsSync(walPath) || fs.existsSync(shmPath)) {
  plaintext.fill(0);
  throw new Error(
    "database sidecar files exist; ensure the Mac Stream is stopped before import",
  );
}
fs.mkdirSync(runtimeRoot, { recursive: true, mode: 0o700 });

let backupPath = null;
if (fs.existsSync(targetPath)) {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "");
  backupPath = `${targetPath}.pre-cutover-${stamp}`;
  fs.copyFileSync(targetPath, backupPath, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(backupPath, 0o600);
}

const temporaryPath = path.join(
  runtimeRoot,
  `.dingtalk.db.tmp-${process.pid}-${crypto.randomBytes(6).toString("hex")}`,
);
fs.writeFileSync(temporaryPath, plaintext, { mode: 0o600, flag: "wx" });
fs.chmodSync(temporaryPath, 0o600);
fs.renameSync(temporaryPath, targetPath);
fs.chmodSync(targetPath, 0o600);
plaintext.fill(0);

console.log(
  `DingTalk state database imported and authenticated; ` +
    `${backupPath ? "one backup was created" : "no previous database existed"}; ` +
    `no message content was printed.`,
);
