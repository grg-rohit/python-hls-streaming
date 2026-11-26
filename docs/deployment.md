# Terraform + Docker Compose deployment

## Local (Windows Docker Desktop or Linux)

Copy `app/.env.example` to `app/.env` and supply your existing S3-compatible
bucket credentials and a strong random worker token. Use separate development
buckets to avoid modifying production metadata. No Terraform or Kubernetes is
needed locally. The current repository requires S3-compatible storage; it is
not offline by default.

CPU: `docker compose --profile cpu-worker up -d --build`

GPU: `docker compose --profile gpu up -d --build` (requires a compatible NVIDIA
GPU, drivers and Docker GPU support; the inherited PyTorch/CUDA pins must build).

Open http://localhost:8000. Check `docker compose ps` and
`docker compose logs -f`. Plain `docker compose up` starts only the API.

## AWS production (single CPU host baseline)

Prerequisites: Terraform, AWS CLI credentials authorized to provision EC2/VPC,
an SSH key, and existing private S3 buckets with appropriately scoped credentials.
Terraform does not create or modify your storage buckets or upload your secrets.
EC2 and its disk incur charges. GPU hosts require separate driver/runtime setup.

1. In `terraform/`, copy `terraform.tfvars.example` to `terraform.tfvars` and
   edit the region, instance type, disk size and trusted SSH CIDR.
2. Run `terraform init`, `terraform validate`, `terraform plan -out=hls.tfplan`.
   Review the plan and costs, then run `terraform apply hls.tfplan`.
3. Obtain the IP using `terraform output -raw public_ip`. SSH as `ubuntu` and
   run `sudo cloud-init status --wait` and `sudo docker compose version`.
   Compose 2.24.4 or newer is required for the production override.
4. Transfer the project ZIP (without secrets) using SCP, unzip it on the host,
   enter `python-hls-streaming`, create `app/.env` from the example and fill in
   credentials securely. Run `chmod 600 app/.env`. Set APP_USERNAME and
   APP_PASSWORD for production. Never commit secrets or Terraform state.
5. Run:
   ```sh
   sudo docker compose -f docker-compose.yml -f docker-compose.production.yml --profile cpu-worker up -d --build
   sudo docker compose -f docker-compose.yml -f docker-compose.production.yml ps
   curl --fail http://127.0.0.1:8000/health
   ```
6. From your computer run `ssh -L 8000:127.0.0.1:8000 ubuntu@SERVER_IP`, then
   open http://localhost:8000. The application is not publicly exposed by default.
   For public access, configure an authenticated HTTPS reverse proxy or tunnel
   separately. Do not expose plain HTTP or worker credentials.

Application updates: transfer new source, keep the environment file and worker
cache, then repeat the Compose build/up command. Stop old API/workers before
switching metadata versions. Keep only one API process per metadata bucket:
the existing catalog uses a process-local lock. Do not run the old Kubernetes
API and the new Compose API against the same bucket concurrently.

Terraform destroy deletes the EC2 infrastructure and its root disk, not the
existing S3 buckets. Back up any local data first. Securely retain state;
configure a protected remote state backend before collaborating.

This baseline does not provide multi-host orchestration, automatic GPU setup,
autoscaling, TLS provisioning, or automatic application releases. The cloud
and local Docker builds must be tested in their target environments.
