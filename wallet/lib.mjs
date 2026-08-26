// Shared helpers for the local-keypair fallback wallet scripts.
// See ../docs/LOCAL_WALLET_SETUP.md for why this exists (Phantom MCP's
// browser-based device-auth flow was confirmed stuck/broken on Phantom's
// own backend -- see docs/PHANTOM_MCP_SETUP.md's troubleshooting section --
// so this is a separate execution path, not a replacement for it).
//
// This is a DIFFERENT wallet identity than whatever Phantom MCP's embedded
// wallet holds -- MPC/embedded wallets (which is what Phantom MCP creates)
// have no exportable raw private key by design, so this can never be the
// *same* wallet as an existing Phantom-MCP-created one. Treat it as its own
// fresh, separately-funded wallet.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Connection, Keypair } from "@solana/web3.js";
import bs58 from "bs58";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(__dirname, "..");

// Minimal hand-rolled .env parser -- deliberately not a dependency, this is
// a security-sensitive module and every extra dependency is extra surface
// for a file that ends up holding a live private key. Existing process env
// vars always win (lets CI/deploy environments override without editing
// the file), same precedence convention as most .env loaders.
function loadEnvFile() {
  const envPath = path.join(REPO_ROOT, ".env");
  const env = {};
  if (!existsSync(envPath)) return env;
  for (const rawLine of readFileSync(envPath, "utf-8").split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    env[key] = value;
  }
  return env;
}

function getEnv(name) {
  if (process.env[name] !== undefined) return process.env[name];
  const fileEnv = loadEnvFile();
  return fileEnv[name];
}

/**
 * Loads the local signing keypair from AGENT_WALLET_PRIVATE_KEY (.env or
 * process env). Accepts either Phantom's "Export Private Key" base58
 * string format, or a solana-keygen-style JSON array of 64 bytes.
 *
 * Throws with a clear, actionable message rather than a raw parse error --
 * this is the one thing every script in this directory depends on, and a
 * cryptic failure here (over real money) is worse than an obvious one.
 */
export function loadKeypair() {
  const raw = getEnv("AGENT_WALLET_PRIVATE_KEY");
  if (!raw || !raw.trim()) {
    throw new Error(
      "AGENT_WALLET_PRIVATE_KEY is not set. Copy .env.example to .env at the repo root and fill it in " +
        "(never commit .env -- it's already gitignored). See docs/LOCAL_WALLET_SETUP.md.",
    );
  }
  const trimmed = raw.trim();
  let secretKey;
  try {
    if (trimmed.startsWith("[")) {
      secretKey = Uint8Array.from(JSON.parse(trimmed));
    } else {
      secretKey = bs58.decode(trimmed);
    }
  } catch (err) {
    throw new Error(
      `AGENT_WALLET_PRIVATE_KEY is set but couldn't be parsed as base58 or a JSON byte array: ${err.message}`,
    );
  }
  try {
    return Keypair.fromSecretKey(secretKey);
  } catch (err) {
    throw new Error(`AGENT_WALLET_PRIVATE_KEY decoded but is not a valid Solana secret key: ${err.message}`);
  }
}

export function getConnection() {
  const rpcUrl = getEnv("SOLANA_RPC_URL") || "https://api.mainnet-beta.solana.com";
  return new Connection(rpcUrl, "confirmed");
}
