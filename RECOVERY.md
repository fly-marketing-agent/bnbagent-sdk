# Recovery Guide: AgenticCommerce Storage Layout Bug (V2 Rescue Implementation)

## Problem Summary

After upgrading an AgenticCommerce proxy to the V2 rescue implementation (`AgenticCommerceRescue`), the `onlyOwner` modifier reads from storage **slot 0** (which contains the `paymentToken` address) instead of **slot 2** (which contains the `owner` wallet address). This causes the contract to become self-locked — no real wallet can pass the ownership check.

## Root Cause

The V2 rescue implementation uses `Ownable` from OpenZeppelin, which stores `_owner` at slot 0. However, in the proxy's storage layout (inherited from the original `AgenticCommerce` implementation), slot 0 is occupied by `paymentToken` and slot 2 contains the actual `owner` address. This storage layout collision means `owner()` returns the USDT/payment token address instead of the real owner.

This issue was identified on testnet in PR #20 as "legacy Ownable storage layout incompatible with AccessControl version" but was not remediated for mainnet deployments.

## Impact

- **All independently deployed AgenticCommerce instances** that upgrade to V2 rescue impl are affected
- The `deploy.ts` script's `owner()` check (line 48-50) also fails under V2 impl
- User funds in the contract become permanently locked (no owner-gated function can be called)

## Verified Solution

Upgrade the proxy implementation from V2 to V3. The V3 implementation correctly reads owner from slot 2 (using `AccessControl` instead of `Ownable`).

### Verification Steps

1. Check current implementation: `get_storage_at(proxy, IMPLEMENTATION_SLOT)` → V2 address
2. Simulate upgrade with `eth_call` + state override setting impl to V3
3. Call `rescueERC20(token, recipient, amount)` from owner wallet → SUCCESS
4. Execute the actual upgrade on-chain
5. Verify rescue was successful by checking token balances

### Example Recovery Transaction Flow

```
1. upgradeToAndCall(V3_IMPL_ADDRESS, "0x") — from real owner wallet
2. rescueERC20(USDT, ownerWallet, lockedAmount) — from real owner wallet
3. Verify USDT balance returned to owner wallet
```

## Prevention

- Always verify storage layout compatibility before UUPS upgrades
- Use `AccessControl`-based ownership (slot 2+) instead of `Ownable` (slot 0) in proxy implementations
- Add storage layout validation to the deploy/upgrade scripts

## References

- GitHub Issue: https://github.com/bnb-chain/bnbagent-sdk/issues/35
- PR #20 (testnet bug identification): https://github.com/bnb-chain/bnbagent-sdk/pull/20
- Affected contract on BSC: `0x7D7043bC1a308e245e56C8FbD9144FCdf2285aA1`
