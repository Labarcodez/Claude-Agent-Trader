#!/usr/bin/env node
// Prints SOL + SPL token balances for the local wallet as JSON.
// Usage: node wallet/balance.mjs
import { LAMPORTS_PER_SOL, PublicKey } from "@solana/web3.js";
import { loadKeypair, getConnection } from "./lib.mjs";

const TOKEN_PROGRAM_ID = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");

async function main() {
  const keypair = loadKeypair();
  const connection = getConnection();

  const lamports = await connection.getBalance(keypair.publicKey);
  const solBalance = lamports / LAMPORTS_PER_SOL;

  const tokenAccounts = await connection.getParsedTokenAccountsByOwner(keypair.publicKey, {
    programId: TOKEN_PROGRAM_ID,
  });

  const tokens = tokenAccounts.value
    .map(({ account }) => account.data.parsed.info)
    .filter((info) => Number(info.tokenAmount.uiAmount) > 0)
    .map((info) => ({
      mint: info.mint,
      amount: info.tokenAmount.uiAmount,
      decimals: info.tokenAmount.decimals,
    }));

  console.log(
    JSON.stringify(
      {
        address: keypair.publicKey.toBase58(),
        sol: solBalance,
        lamports,
        tokens,
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
