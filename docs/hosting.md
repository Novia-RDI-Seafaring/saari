# Hosting saari (Azure pilot guide)

How to run saari as an org-internal, multi-user service. Target shape:
Azure Container Apps + Entra ID "Easy Auth" + one Azure Files share.
This guide is a checklist for a pilot, not hardened production ops.

## Model

- saari stays a **single-writer, files-first** system. One container
  replica; per-user project directories on one share:

  ```
  /data/<entra-object-id>/<project>/.saaristo/   # SQLite DB + raw JSON
  /data/<entra-object-id>/<project>/papers/      # exports (user-visible)
  ```

- **Authentication is the platform's job.** Easy Auth validates the Entra
  token at the ingress and injects `x-ms-client-principal-id`; saari trusts
  that header (see README "Hosted mode"). Never expose the container
  without the auth layer.
- Two processes from the same image:
  - `saari serve` (default CMD) - REST API + web UI, port 3044.
  - `saari-mcp --http` - MCP streamable HTTP for Copilot Studio / remote
    MCP hosts, port 3045.

## Steps

1. **Build and push the image**

   ```bash
   az acr create -g <rg> -n <registry> --sku Basic
   az acr build -r <registry> -t saari:0.1.0 .
   ```

2. **Storage**

   ```bash
   az storage account create -g <rg> -n <account> --sku Premium_LRS --kind FileStorage
   az storage share-rm create --storage-account <account> -n saari-data --enabled-protocols NFS
   ```

   NFS (not SMB) matters: SQLite file locking is unreliable over SMB.
   Keep **one replica** (`--min-replicas 1 --max-replicas 1`) regardless.

3. **Container App**

   ```bash
   az containerapp env create -g <rg> -n saari-env
   az containerapp create -g <rg> -n saari --environment saari-env \
     --image <registry>.azurecr.io/saari:0.1.0 \
     --target-port 3044 --ingress external \
     --min-replicas 1 --max-replicas 1
   # then mount the share at /data (containerapp env storage set + volume mount)
   ```

   Set the liveness/readiness probe to `GET /api/health` (it is exempt
   from the identity requirement).

4. **Easy Auth (the critical step)**

   ```bash
   az containerapp auth microsoft update -g <rg> -n saari \
     --client-id <app-registration-id> --tenant-id <tenant>
   az containerapp auth update -g <rg> -n saari \
     --unauthenticated-action Return401
   ```

   Needs an Entra **app registration** (usually an IT ticket - request it
   early). After this, every request that reaches saari carries
   `x-ms-client-principal-id`, and per-user directories appear on first
   touch. `Return401` (not redirect) so API/MCP clients get clean failures.

5. **MCP endpoint for Copilot Studio** - second container app from the
   same image with command `saari-mcp --http --host 0.0.0.0 --port 3045`,
   same volume, same auth. Register `https://<app-url>/mcp` as an MCP
   tool/connector in Copilot Studio (licensing for Copilot Studio is per
   org - verify with IT).

6. **Politeness/limits** - set `OPENALEX_MAILTO` (all users share one
   egress IP), and put a size quota or cleanup policy on /data.

## Local smoke test (no Azure needed)

```bash
docker build -t saari .
docker run --rm -p 3044:3044 -v /tmp/saari-data:/data saari
curl http://localhost:3044/api/health                      # 200, no identity
curl -H 'x-saari-user: alice' http://localhost:3044/api/project   # scoped root
```

`x-saari-user` stands in for the Easy Auth header during local testing.

## Known limitations (pilot)

- Single replica by design (SQLite single-writer). Scale later = one
  container per user-shard, or move indexes to a server DB.
- No per-user disk quotas yet.
- The in-app chat agent needs an LLM API key (`OPENAI_API_KEY` /
  `ANTHROPIC_API_KEY` as a Container Apps secret) - decide whose budget
  that is, or leave it unset (the UI works without it; Copilot/Claude
  users bring their own brain).
- OneDrive export of results is not built yet; the Graph API call is the
  natural extension once the app registration exists.
