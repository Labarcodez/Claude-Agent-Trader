#!/usr/bin/env node
// Local-keypair Jupiter swap. Defaults to quote-only (no funds move, no
// signature, nothing broadcast) -- pass --execute to actually sign and
// send. This mirrors Phantom MCP's own buy_token(execute=false) default on
// purpose: an accidental/casual run of this script must never be able to
// move money by itself.
//
// IMPORTANT: an AI agent should never pass --execute on your behalf.
// Executing a real swap is something you run yourself, deliberately, in
// your own terminal -- see docs/LOCAL_WALLET_SETUP.md.
//
// Usage:
//   node wallet/swap.mjs <inputMint> <outputMint> <amountBaseUnits> [--slippage-bps N]              # quote only
//   node wallet/swap.mjs <inputMint> <outputMint> <amountBaseUnits> [--slippage-bps N] --execute     # signs & sends
import { VersionedTransaction } from "@solana/web3.js";
import { loadKeypair, getConnection } from "./lib.mjs";

let execute = false;
let slippageBps = "100";
const positional = [];
const rawArgs = process.argv.slice(2);
for (let i = 0; i < rawArgs.length; i++) {
  const arg = rawArgs[i];
  if (arg === "--execute") {
    execute = true;
  } else if (arg === "--slippage-bps") {
    slippageBps = rawArgs[++i];
  } else {
    positional.push(arg);
  }
}
const [inputMint, outputMint, amount] = positional;

if (!inputMint || !outputMint || !amount) {
  console.error(
    "Usage: node wallet/swap.mjs <inputMint> <outputMint> <amountBaseUnits> [--slippage-bps N] [--execute]",
  );
  process.exit(1);
}

// https://api.jup.ag/swap/v1 -- the old quote-api.jup.ag/v6 host and the
// intermediate lite-api.jup.ag host are both deprecated (lite-api.jup.ag as
// of 2026-01-31); confirmed live 2026-08-21 that quote-api.jup.ag no longer
// even resolves in DNS, while this endpoint works.
const JUP_SWAP_BASE = "https://api.jup.ag/swap/v1";

async function getQuote() {
  const url =
    `${JUP_SWAP_BASE}/quote?inputMint=${inputMint}&outputMint=${outputMint}` +
    `&amount=${amount}&slippageBps=${slippageBps}`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Jupiter quote request failed: HTTP ${res.status} ${await res.text()}`);
  }
  return res.json();
}

async function main() {
  const quote = await getQuote();
  const priceImpactPct = Number(quote.priceImpactPct || 0) * 100;
  console.error(
    `Quote: ${quote.inAmount} (in) -> ${quote.outAmount} (out), price impact ${priceImpactPct.toFixed(3)}%`,
  );

  if (!execute) {
    console.log(JSON.stringify({ mode: "quote-only", executed: false, quote }, null, 2));
    return;
  }

  // Everything below here signs and broadcasts a real transaction.
  const keypair = loadKeypair();
  const connection = getConnection();

  const swapRes = await fetch(`${JUP_SWAP_BASE}/swap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      quoteResponse: quote,
      userPublicKey: keypair.publicKey.toBase58(),
      wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
      prioritizationFeeLamports: "auto",
    }),
  });
  if (!swapRes.ok) {
    throw new Error(`Jupiter swap-transaction build failed: HTTP ${swapRes.status} ${await swapRes.text()}`);
  }
  const { swapTransaction } = await swapRes.json();

  const tx = VersionedTransaction.deserialize(Buffer.from(swapTransaction, "base64"));
  tx.sign([keypair]);

  const signature = await connection.sendRawTransaction(tx.serialize(), {
    skipPreflight: false,
    maxRetries: 3,
  });
  console.error(`Submitted: ${signature}`);
  const latestBlockhash = await connection.getLatestBlockhash();
  await connection.confirmTransaction({ signature, ...latestBlockhash }, "confirmed");

  console.log(
    JSON.stringify(
      {
        mode: "executed",
        executed: true,
        signature,
        explorer: `https://solscan.io/tx/${signature}`,
        quote,
      },
      null,
      2,
    ),
  );
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
