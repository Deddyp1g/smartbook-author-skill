# Temporary Huawei Cloud preview — experimental

This repository contains a **simulation and integration prototype**, not an operational cloud-test-and-upload workflow. The real Huawei adapter is disabled until independently verified resource ownership, browser verification, cleanup, cloud-side deletion verification, and recovery are implemented and tested together. Do not attempt to enable it by editing its state or substituting the direct uploader to bypass the gate.

The external [auto-deploy repository](https://gitcode.com/weixin_43883336/auto-deploy) is not bundled. The inspected revision is `971a801cf0917f2e86365d09b0ed58a93ca27262`; its repository did not include a license when inspected. Review its terms independently before use. Its Terraform state may include an imported pre-existing keypair and its destroy operation affects all state-managed resources. An empty Terraform state is not independent proof that billable cloud resources are gone. Do not use this prototype to create resources.

## Intended workflow

1. Build and validate the complete book using the package guide. The author supplies the source and has rights to its content.
2. Preserve the exact ZIP snapshot and extract only that snapshot into the temporary preview staging area. Keep the platform API key and source files off the preview host.
3. Once implemented and explicitly authorized for a bounded cloud budget, provision a uniquely identified, temporary ECS with restricted access. Exercise chapter navigation, interactions, mobile layout and console errors against the deployed snapshot. Record the snapshot digest, device/viewport and results.
4. Destroy only demonstrably run-owned resources. Independently query the correct cloud account and region until all billable resources are confirmed absent; an error, partial apply, lost state, unknown ownership or incomplete inventory is not success. Retain recovery records on failure or interruption.
5. Only after success, upload the same snapshot locally using an authorized author key and specified existing book ID. A successful upload is not approval or publication. The temporary static preview is not the platform's restricted reader: separately test the actual platform reader and record that status.

## Prototype preparation

Python 3.9 or newer is required. From this repository's root:

```bash
python skills/smartbook-author/scripts/smartbook.py validate /path/to/book.zip
python skills/smartbook-author/scripts/cloud_pipeline.py prepare /path/to/book.zip --run-dir /path/to/new-run-directory
```

Preparation validates and copies the ZIP, makes a preview staging directory and writes its digest. The target directory must not already exist. The directory is internal working material, not an independently authenticated audit record; protect it from modification, and do not publish it without checking for private data. `execute --simulation` is for tests using a fake adapter; its success cannot authorize an upload. The previous standalone uploader remains available for separate workflows, but does not satisfy the cloud-testing requirement.

## Recovery and limits

No temporary ECS has been created or tested by this prototype. Local tests cannot establish Huawei IAM permissions, costs, resource inventory completeness, browser behavior or actual destruction. If a future run fails after any cloud request, retain the complete run directory and inspect the cloud console for all potentially created resources; do not destroy resources of uncertain ownership. Resume only after independently establishing ownership and cleanup. Never expose API keys in logs, URLs, screenshots, or generated assets.
