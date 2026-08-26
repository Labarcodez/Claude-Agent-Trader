#!/usr/bin/env node
// Prints the local wallet's public Solana address -- safe to share, fund,
// and paste anywhere. Never prints the private key.
import { loadKeypair } from "./lib.mjs";

try {
  const keypair = loadKeypair();
  console.log(keypair.publicKey.toBase58());
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exit(1);
}
