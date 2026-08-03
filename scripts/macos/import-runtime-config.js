#!/usr/bin/env node

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const [encryptedPath, privateKeyPath, difyResourcesPath, projectRootInput] =
  process.argv.slice(2);
if (!encryptedPath || !privateKeyPath || !difyResourcesPath || !projectRootInput) {
  console.error(
    "Usage: node import-runtime-config.js <encrypted-config> <private-key> " +
      "<dify-resources> <project-root>",
  );
  process.exit(2);
}

function readJson(filePath, label) {
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (_) {
    throw new Error(`${label} is missing or invalid`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return parsed;
}

function requireText(value, label) {
  if (
    typeof value !== "string" ||
    value.trim() === "" ||
    value.includes("\n") ||
    value.includes("\r")
  ) {
    throw new Error(`${label} is missing or invalid`);
  }
  return value.trim();
}

function atomicWrite(filePath, content, mode) {
  const directory = path.dirname(filePath);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporaryPath = path.join(
    directory,
    `.${path.basename(filePath)}.tmp-${process.pid}-${crypto.randomBytes(6).toString("hex")}`,
  );
  fs.writeFileSync(temporaryPath, content, {
    encoding: "utf8",
    mode,
    flag: "wx",
  });
  fs.chmodSync(temporaryPath, mode);
  fs.renameSync(temporaryPath, filePath);
  fs.chmodSync(filePath, mode);
}

function backupIfPresent(filePath, stamp) {
  if (!fs.existsSync(filePath)) {
    return null;
  }
  const backupPath = `${filePath}.pre-mac-migration-${stamp}`;
  fs.copyFileSync(filePath, backupPath, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(backupPath, 0o600);
  return backupPath;
}

function mergeEnv(sourceText, updates) {
  const lines = sourceText.replace(/\r\n/g, "\n").split("\n");
  const seen = new Set();
  const merged = lines.map((line) => {
    const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=/);
    if (!match || !Object.prototype.hasOwnProperty.call(updates, match[1])) {
      return line;
    }
    const name = match[1];
    seen.add(name);
    return `${name}=${updates[name]}`;
  });
  const missing = Object.keys(updates).filter((name) => !seen.has(name));
  if (missing.length > 0) {
    if (merged.length > 0 && merged[merged.length - 1] !== "") {
      merged.push("");
    }
    merged.push("# Mac production runtime configuration");
    for (const name of missing.sort()) {
      merged.push(`${name}=${updates[name]}`);
    }
  }
  return `${merged.join("\n").replace(/\n+$/, "")}\n`;
}

const projectRoot = path.resolve(projectRootInput);
const envExamplePath = path.join(projectRoot, ".env.example");
if (!fs.statSync(projectRoot).isDirectory() || !fs.existsSync(envExamplePath)) {
  throw new Error("project root is invalid");
}

const envelope = readJson(encryptedPath, "encrypted configuration");
if (
  envelope.version !== 1 ||
  envelope.purpose !== "dingtalk-runtime-config" ||
  envelope.key_algorithm !== "RSA-OAEP-SHA256" ||
  envelope.content_algorithm !== "AES-256-GCM"
) {
  throw new Error("encrypted configuration has an unsupported format");
}

let payload;
try {
  const privateKey = fs.readFileSync(privateKeyPath, "utf8");
  const aesKey = crypto.privateDecrypt(
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
  const plaintext = Buffer.concat([
    decipher.update(Buffer.from(envelope.ciphertext, "base64")),
    decipher.final(),
  ]);
  payload = JSON.parse(plaintext.toString("utf8"));
  plaintext.fill(0);
  aesKey.fill(0);
} catch (_) {
  throw new Error("encrypted configuration could not be authenticated or decrypted");
}

if (
  payload.schema_version !== 1 ||
  !payload.environment ||
  !payload.enterprise_config ||
  !payload.store_directory_config
) {
  throw new Error("decrypted configuration failed validation");
}

const requiredDingTalk = [
  "DINGTALK_CLIENT_ID",
  "DINGTALK_CLIENT_SECRET",
  "DINGTALK_ALLOWED_USERS",
  "DINGTALK_HANDLER_USERS",
  "DINGTALK_OPERATOR_USERS",
  "DINGTALK_DOMAIN_HANDLERS",
  "DINGTALK_ROBOT_CODE",
];
const updates = {};
for (const [name, value] of Object.entries(payload.environment)) {
  if (!/^[A-Z][A-Z0-9_]*$/.test(name) || typeof value !== "string") {
    throw new Error("decrypted environment contains an invalid entry");
  }
  if (value.includes("\n") || value.includes("\r")) {
    throw new Error("decrypted environment contains an invalid value");
  }
  updates[name] = value;
}
for (const name of requiredDingTalk) {
  requireText(updates[name], name);
}

const dify = readJson(difyResourcesPath, "Dify resources");
updates.DIFY_BASE_URL = "http://127.0.0.1:8081";
updates.DIFY_CONSOLE_PUBLIC_URL = "http://127.0.0.1:8081";
updates.DIFY_DOCKER_BASE_URL = "http://host.docker.internal:8081";
updates.DIFY_DATASET_ID = requireText(dify.dataset_id, "Dify dataset ID");
updates.DIFY_DATASET_API_KEY = requireText(
  dify.dataset_api_key,
  "Dify dataset API key",
);
updates.DIFY_INDEXING_TECHNIQUE = "economy";
updates.DIFY_APP_ID = requireText(dify.app_id, "Dify app ID");
updates.DIFY_APP_API_KEY = requireText(dify.app_api_key, "Dify app API key");

if (
  payload.enterprise_config.schema_version !== 1 ||
  !payload.enterprise_config.assistant ||
  !payload.enterprise_config.dify
) {
  throw new Error("enterprise configuration failed validation");
}
if (
  !payload.store_directory_config.baseId ||
  !payload.store_directory_config.tableId ||
  !payload.store_directory_config.fieldIds
) {
  throw new Error("store directory configuration failed validation");
}
payload.store_directory_config.command = "/Users/smallworks/.local/bin/dws";

const envPath = path.join(projectRoot, ".env");
const enterprisePath = path.join(projectRoot, "config", "enterprise.local.json");
const storePath = path.join(projectRoot, "config", "store-directory.local.json");
const baseEnv = fs.existsSync(envPath)
  ? fs.readFileSync(envPath, "utf8")
  : fs.readFileSync(envExamplePath, "utf8");
const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "");
const backups = [envPath, enterprisePath, storePath]
  .map((filePath) => backupIfPresent(filePath, stamp))
  .filter(Boolean);

atomicWrite(envPath, mergeEnv(baseEnv, updates), 0o600);
atomicWrite(
  enterprisePath,
  `${JSON.stringify(payload.enterprise_config, null, 2)}\n`,
  0o600,
);
atomicWrite(
  storePath,
  `${JSON.stringify(payload.store_directory_config, null, 2)}\n`,
  0o600,
);

console.log(
  `Mac runtime configuration imported: ${Object.keys(updates).length} environment entries; ` +
    `${backups.length} backup(s) created; no secret values were printed.`,
);
