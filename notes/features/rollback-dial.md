# Rollback Dial Feature

## Problem

The current rollback implementation doesn't work like a dial. It:
1. Gets the currently deployed hash from history
2. Filters out that hash from backups
3. Always picks the **most recent** backup that isn't the current version

This creates a ping-pong effect:
- If you have versions A, B, C (oldest to newest) and C is deployed
- First rollback: C -> B (filters C, picks B as newest remaining)
- Second rollback: B -> C (filters B, picks C as newest remaining - NOT A!)

## Desired Behavior

Rollback should work like a dial:
1. Each rollback moves one step back in the version history
2. Once you hit the oldest version, it should stop (not wrap)
3. Versions should be ordered chronologically

Example with versions A, B, C (oldest to newest), C deployed:
- First rollback: C -> B
- Second rollback: B -> A
- Third rollback: Error "Already at oldest version"

## Implementation Approach

Find the currently deployed version's position in the sorted backup list, then select the **next older** version (not just "newest that isn't current").

Key insight: Backups are sorted newest-first. So if current version is at index i, we want index i+1 (one step older).

## Edge Cases

1. No backups at all -> Error "No backups available"
2. Current version is already the oldest -> Error "Already at oldest version"
3. Current version is not in backups (fresh deploy) -> Just go to most recent backup (index 0)
4. Explicit version specified -> Honor that request regardless of dial position
