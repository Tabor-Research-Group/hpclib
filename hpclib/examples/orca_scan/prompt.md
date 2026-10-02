# Example prompt

Paste this into Claude Desktop or Claude Code once the tunnel is up and the `hpclib` MCP server is configured
(see README.md). Adjust the paths.

> I generated a 5 × 5 ORCA scan in `/Users/me/Desktop/scans/sample_scan` with Psience's `ScanManager`
> (`scan_info.json` plus one `.inp` per point, B97-3c optimizations with 4 cores each).
>
> 1. Check `cluster_info` and read the `orca` guide.
> 2. Push the scan to `scans/sample_scan` on the cluster.
> 3. Do a dry run of the `orca` template over `scan_info.json` with `nprocs` 4 and the label `sample_scan`,
>    and show me the plan and SLURM's start estimate before submitting.
> 4. After I confirm, submit it with an idempotency key, check back on it, and tell me which points failed and
>    why (from the end of their `.out` files).
> 5. Pull the `.out` and `.xyz` files back into the scan directory when it finishes.
>
> If ORCA isn't loaded on the cluster, find the right modules with `search_modules` and propose an updated
> `orca` template instead of working around it.
