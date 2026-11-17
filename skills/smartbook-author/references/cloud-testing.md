# Temporary Huawei Cloud preview and upload

This workflow uses [auto-deploy](https://gitcode.com/weixin_43883336/auto-deploy) at revision `971a801cf0917f2e86365d09b0ed58a93ca27262`. On first cloud deployment the skill downloads that revision into a local cache and reuses it on later runs. The upstream repository is not redistributed here and did not contain a license when inspected; check its terms before use. The automatic download and cloud workflow have not been tested against a real cloud account.

## Requirements

- Python 3.10+, Git, Bash, SSH/SCP, Terraform 1.8+, Node.js and the Playwright Node package with a Chromium browser. On Windows use a shell environment supported by auto-deploy, such as Git Bash. Ensure the Bash executable actually works; the Windows WSL launcher alone is not a shell installation.
- Huawei Cloud credentials as `HW_ACCESS_KEY` and `HW_SECRET_KEY` (or the upstream-supported `HUAWEICLOUD_ACCESS_KEY` and `HUAWEICLOUD_SECRET_KEY`), permissions and quota for ECS, EIP, VPC and related resources.
- Set `SMARTBOOK_CLOUD_REGION` to the cloud region and `SMARTBOOK_CLOUD_CIDR` to the public IPv4 address of the machine running the browser and SSH in `/32` notation. The first run also needs network access to GitCode and Git on PATH.
- By default the pinned checkout is cached in `~/.cache/smartbook-author/auto-deploy-<revision>`. Set `SMARTBOOK_CACHE_DIR` to use a different cache root, or optionally set `SMARTBOOK_AUTO_DEPLOY` to a clean checkout of the pinned revision. The explicit checkout takes precedence and is not modified.
- For upload, set `SMARTBOOK_API_KEY` locally to an author API key with edit access to the specified existing book. It is excluded from the deploy and browser subprocesses. The platform origin and book ID must be supplied by the author.

Install the Playwright package where the Node.js process can resolve it from the installed skill's `scripts` directory. For example, from the installed skill directory run `npm install --no-save --no-package-lock playwright`, then `npx playwright install chromium`. Alternatively, install Playwright in an accessible Node module directory and set `NODE_PATH` to that directory; global npm packages are not automatically resolved on every platform. An existing Chromium-compatible executable can be selected with `--browser-executable`. Installing Playwright does not install Terraform or the other prerequisites.

Example environment setup in Bash (obtain credentials through your usual secure local method):

```bash
export SMARTBOOK_CLOUD_REGION=cn-north-4
export SMARTBOOK_CLOUD_CIDR=203.0.113.10/32
```

## Run the workflow

Resolve these script paths relative to the installed skill directory. Start with a complete, locally validated ZIP. Choose a new run directory; it retains the exact ZIP snapshot, Terraform state and reports.

The pinned auto-deploy upload omits any path component named `.git`, `.auto-deploy`, `node_modules`, `.venv`, `venv`, `target`, `build`, `dist`, or `.env`. Preparation rejects an archive containing these components; rename those paths in the book and update references before packaging again. This additional temporary-deployment constraint is separate from the platform's ZIP validation rules.

```bash
python scripts/smartbook.py validate /path/to/book.zip
python scripts/cloud_pipeline.py prepare /path/to/book.zip --run-dir /path/to/new-run
python scripts/cloud_pipeline.py staged-start --run-dir /path/to/new-run
```

`staged-start` prints the preview endpoint and snapshot SHA-256. Create a JSON scenario containing interactions relevant to this particular book. Each `expect_visible` element must be hidden before the click and visible afterwards:

```json
{"steps":[{"name":"Open chapter two","click":"#chapter-2","expect_visible":"#chapter-2-content"}]}
```

Run the checks, then validate the report and destroy the temporary resources. Use the endpoint returned by `staged-start`; `--browser-executable` may point to an installed Chromium-compatible browser.

```bash
python scripts/browser_verify.py --run-dir /path/to/new-run --url http://PREVIEW_IP:5000/ --scenario /path/to/scenario.json
python scripts/cloud_pipeline.py staged-verify --run-dir /path/to/new-run
python scripts/cloud_pipeline.py staged-cleanup --run-dir /path/to/new-run
python scripts/cloud_pipeline.py upload --run-dir /path/to/new-run --book-id 123 --base-url http://example.com:5173
```

The browser check covers the supplied interactions at desktop and mobile sizes, horizontal overflow, failed requests, console errors and the served index. It does not establish that every chapter, interaction, animation, or target-reader restriction works. The harness should inspect all chapters and test the platform reader separately. On a browser or verification failure, still run `staged-cleanup`; uploading remains unavailable until verification and cleanup succeed. Upload uses the same ZIP digest, preserves the idempotency key for retries, and does not approve or publish the book.

## Cleanup and recovery

The deployer creates billable resources. `staged-cleanup` invokes upstream `destroy.py` against this run's Terraform state and checks the state has no remaining managed instances. This is reported as `terraform_state_empty`, not an independent cloud-side inventory; check the Huawei Cloud console for residual resources, especially after interruption or partial provisioning. The working directory may contain private keys and state: retain it for recovery and do not publish it.

If deployment is interrupted, first ensure its deploy, SSH and Terraform processes have stopped, then set `SMARTBOOK_RECOVERY_PROCESSES_STOPPED=yes` and run `staged-cleanup`. If ownership cannot be determined or cleanup fails, inspect the retained work directory and cloud resources; do not claim the run has been cleaned. Re-run cleanup after resolving the issue. The original direct uploader remains available for workflows that did not request cloud testing.
