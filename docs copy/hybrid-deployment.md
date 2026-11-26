# Release 1: AWS CPU + local GPU

## Architecture and scope

The AWS API is the sole writer of the S3 metadata catalog. The AWS CPU worker
claims standard HLS jobs; the Windows/Docker Desktop GPU worker claims AI
upscale jobs over the API. Both workers access the same production original and
stream buckets directly. Uploaded videos and published HLS renditions remain
available when the GPU computer is off. New AI jobs wait in the queue until it
returns. Interrupted jobs are recovered according to the existing lease timeout.
The existing upload flow is unchanged: upload, request normal HLS conversion,
then optionally request AI upscaling for eligible source resolutions.

Production Compose enables `HYBRID_DEPLOYMENT=true` and `AI_REQUIRED_DEVICE=cuda`.
The policy forces all normal conversion modes to CPU and AI inference to CUDA;
queued or expired legacy jobs are normalized at claim time. A valid in-flight
lease is not reassigned. Standalone development keeps the original routing
unless explicitly opted in. No live streaming changes are included.

## 1. Deploy AWS CPU host

Follow [deployment.md](deployment.md) for Terraform, SSH and storage setup.
Review the EC2, disk, IPv4, storage and data transfer charges before applying;
this configuration is not guaranteed free tier. The default m6i.xlarge is a
paid CPU instance. No GPU instance is required. Retain the existing Terraform
state for subsequent releases. Set `APP_USERNAME`, `APP_PASSWORD` and a strong
random `WORKER_SHARED_TOKEN` in the cloud `app/.env`; never commit them.
For production use distinct buckets from development and private S3 access.
The cloud API and CPU worker must have appropriate S3 permissions.

```sh
sudo docker compose -f docker-compose.yml -f docker-compose.production.yml --profile cpu-worker up -d --build
sudo docker compose -f docker-compose.yml -f docker-compose.production.yml --profile cpu-worker ps
curl --fail http://127.0.0.1:8000/health
```

For an existing deployment, back up the metadata catalog and drain/stop the old
workers and API before replacing code. Do not run a second API against the same
catalog, including a local development API. Keep the existing `app/.env` and
worker data. The existing stop grace period may interrupt long conversions;
allow jobs to finish before stopping if possible.

## 2. Configure local GPU worker (Windows PowerShell)

In the extracted project folder:

```powershell
Copy-Item app/.env.remote-gpu.example app/.env.remote-gpu
notepad app/.env.remote-gpu
```

Enter the SAME production bucket names, region, S3 endpoint if applicable, and
worker token as on AWS. Use your own narrowly scoped S3 credentials; they need
not be the same access keys as the server. The remote worker needs read access
to original videos and write access to generated stream objects. Do not grant
it catalog write access. Existing S3-compatible storage is supported.

The production API is bound to loopback on EC2. For initial private testing,
open a separate PowerShell window and keep this SSH tunnel running:

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:18000:127.0.0.1:8000 ubuntu@YOUR_SERVER_IP
```

Verify from Windows using `curl.exe --fail http://127.0.0.1:18000/health`.
Docker Desktop normally provides `host.docker.internal` for reaching the host.
Verify connectivity from a container before running the GPU worker:

```powershell
docker run --rm curlimages/curl --fail http://host.docker.internal:18000/health
```

If this fails due to Docker Desktop networking, use a private VPN or an HTTPS
reverse proxy reachable from the container. Do not expose an unauthenticated
HTTP API or bind the SSH tunnel publicly to work around the failure. For public
users, configure HTTPS separately; the SSH tunnel only provides private access.
For an HTTPS endpoint, set `$env:REMOTE_API_URL = "https://your-domain.example"`
(the final endpoint, not a redirect). Otherwise use the tunnel default:

```powershell
$env:REMOTE_API_URL = "http://host.docker.internal:18000"
docker compose -p hls-remote-gpu -f docker-compose.remote-gpu.yml up -d --build
docker compose -p hls-remote-gpu -f docker-compose.remote-gpu.yml logs -f worker-gpu
```

The standalone file has no API service and uses a separate scratch directory.
The project name isolates it from local development. Expect a startup log with
`device=cuda capabilities=gpu`. If CUDA is unavailable the worker fails startup
rather than taking CPU jobs. Existing CUDA/PyTorch/FrameLift pins are retained;
confirm driver compatibility and NVENC availability locally. AI benchmarks are
disabled on the remote worker by default. Leave the tunnel running while jobs
are processed. Subsequent starts can omit `--build` unless code changed.

## 3. Acceptance tests

1. Open http://127.0.0.1:18000 in your browser while the tunnel is open.
2. Stop the remote GPU worker. Upload a short 480p or 720p clip and request
   standard HLS. Watch cloud CPU logs and verify playback.
3. Request AI upscaling; verify the job remains queued while GPU is offline.
4. Start the GPU worker; verify the job is claimed locally, downloads from S3,
   produces additional HLS renditions, and reports completion to the AWS API.
5. Stop the GPU worker again and verify previously published videos still play.
6. Verify only one API is running against production buckets. Never use those
   buckets for live-streaming development.

Offline tests (requires the API Python dependencies):

```sh
python -m unittest discover -s tests -v
```

These tests mock the S3 catalog and CUDA availability; they do not provision AWS,
run Docker builds, or encode media. Perform acceptance tests before public use.

## 4. Progressive release

Keep this VOD release separate from the future live-streaming branch. Version
and deploy the cloud API and local GPU worker together when changing the worker
protocol. Keep credentials, production storage and Terraform state outside the
release archive. For rollback stop workers, restore the previous release and
review catalog compatibility; never blindly overwrite a live catalog backup.
