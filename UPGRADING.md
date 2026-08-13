# Upgrading an existing deployment

Steps for updating a Creative Studio deployment that is already running, with the checks needed
to avoid losing data.

Read the **Before you start** section even if you upgrade routinely — the app applies database
migrations automatically on startup, so how far behind your database is matters more than the
size of the code change.

---

## Before you start

### 1. Find out which migrations will run

The backend calls `run_pending_migrations()` during startup, so deploying a newer image applies
**every migration you are behind on**, not only the ones belonging to this change. Some of them
are destructive.

Against your database, using the new code:

```bash
cd backend
alembic current                              # where your database is
alembic heads                                # where the new code expects it
alembic history --verbose -r current:head    # everything that would apply
```

Read that list before going further. In particular, `cb3c4680571b` runs
`op.drop_table("system_settings")`. If it appears, deploying removes that table. Decide whether
that is acceptable rather than discovering it afterwards.

If `current` already equals `heads`, no migrations will run.

### 2. Back up the database

```bash
# Portable, survives instance deletion, restorable into a scratch instance
gcloud sql export sql <INSTANCE> gs://<BUCKET>/backup-$(date +%F).sql \
  --database=creative_studio

# Or an in-place snapshot
gcloud sql backups create --instance=<INSTANCE> --description="pre-upgrade"
```

Prefer the export. Cloud Run rollback is instant and lossless, but **database rollback is not** —
Alembic downgrades are far less exercised than upgrades, and a `drop_table` downgrade recreates
the table empty.

### 3. Rehearse, if the data matters

Restore the export into a temporary instance, point a staging Cloud Run revision at it, and let
the migrations run there first. This is the only way to see the real outcome before it is real.

### 4. Note the current revisions, so you can roll back

```bash
gcloud run revisions list --service=<BACKEND_SERVICE> --region=<REGION> --limit=3
```

The frontend has no Cloud Run equivalent — confirmed by asking directly: `gcloud run services
describe <FRONTEND_SERVICE>` returns `Cannot find service`. It deploys to Firebase Hosting, which
keeps its own release history instead of Cloud Run revisions. Note the current live version there:
```bash
firebase hosting:versions:list --site=<FIREBASE_SITE_ID>
```
See **Rolling back** for why this distinction matters when you actually need to undo a deploy.

---

## Deploying

### If Cloud Build triggers are configured

The usual case if the environment was set up with `bootstrap.sh`. Merge and push; the triggers
build and deploy.

```bash
git fetch <remote> <branch>
git merge <remote>/<branch>
git push origin main
```

### Manually

**Finding the values below.** Every placeholder in these commands — service names, the Firebase
site ID, the frontend URL, and both service accounts — is already sitting on the existing Cloud
Build triggers, since that's what the automated deploy already runs as. Reading it beats guessing
or reconstructing it from Terraform:
```bash
gcloud builds triggers list --region=<REGION> --format="yaml(name,serviceAccount,substitutions)"
```
`<PROJECT_ID>` and `<REGION>` are the project and region you're deploying into; `<INSTANCE>` is
your Cloud SQL instance name (`gcloud sql instances list` if you don't have it handy).

```bash
gcloud builds submit --config backend/cloudbuild.yaml \
  --substitutions=_REGION=<REGION>,_REPO_NAME=<ARTIFACT_REPO>,_SERVICE_NAME=<BACKEND_SERVICE>,SHORT_SHA=$(git rev-parse --short HEAD) \
  --service-account=projects/<PROJECT_ID>/serviceAccounts/<BACKEND_TRIGGER_SA> \
  .

gcloud builds submit --config frontend/cloudbuild-deploy.yaml \
  --substitutions=_BACKEND_SERVICE_ID=<BACKEND_SERVICE>,_BACKEND_URL=<FRONTEND_URL>,_FE_SERVICE_NAME=<FRONTEND_SERVICE>,_FIREBASE_SITE_ID=<FIREBASE_SITE_ID> \
  --service-account=projects/<PROJECT_ID>/serviceAccounts/<FRONTEND_TRIGGER_SA> \
  .
```

`_BACKEND_URL` is the frontend's own Hosting URL, not the backend's — `firebase.json` rewrites `/api/**` on that origin to the Cloud Run backend named by `_BACKEND_SERVICE_ID`, so the Angular app calls same-origin and never hits CORS. It follows the pattern `https://<FIREBASE_SITE_ID>.web.app` unless you've attached a custom domain. Do **not** pass `_ANGULAR_BUILD_COMMAND` or `_FIREBASE_PROJECT_ID`: neither is referenced anywhere in `cloudbuild-deploy.yaml` (the build always runs `npm run build -- --configuration=production`, and the Firebase project comes from the built-in `${PROJECT_ID}`), and a manual submit — unlike a trigger, which tolerates unused ones — rejects the build outright with `key "..." in the substitution data is not matched in the template`. The `--service-account` and staging-bucket caveats above apply here too.

`_FE_SERVICE_NAME`, by contrast, must be present — omit it and you get the mirror-image error,
`key in the template "_FE_SERVICE_NAME" is not matched in the substitution data`, since it *is*
referenced (as a step's `env:` entry). But it has no effect on the result: nothing downstream ever
reads that environment variable. Any non-empty value satisfies it.

Use `cloudbuild-deploy.yaml`, not `frontend/cloudbuild.yaml` — the latter is a thin wrapper the CI trigger uses that only forwards `_FIREBASE_PROJECT_ID` and kicks off the real deploy build `--async`, so it reports success before the actual deploy has even started, and `cloudbuild-deploy.yaml` itself has no default substitutions to fall back on. `cloudbuild-deploy.yaml` is what actually injects secrets, builds Angular, and deploys to Firebase Hosting in one synchronous step — submit it directly for a real manual deploy.

Defaults for the backend's substitutions live at the bottom of `backend/cloudbuild.yaml`.

Two things a plain, undecorated `gcloud builds submit` gets wrong for the backend, both confirmed by actually running it:

- **`SHORT_SHA` is not set.** The image tag in `backend/cloudbuild.yaml` uses `$SHORT_SHA`, which is a Cloud Build *built-in* substitution — populated automatically for a git-triggered build, but empty for a local submit like this one. Without it the build fails at the push step with `invalid image name "...:": could not parse reference`. Pass it explicitly, as above.
- **The default Cloud Build service account usually cannot deploy.** If the environment's Cloud Run service was locked down the way `infra/modules/cloud-run-service` sets it up — a dedicated per-service `<name>-trig@<project>.iam.gserviceaccount.com` holding `roles/run.developer` on that one service, which the Cloud Build *trigger* runs as — a manual submit with no `--service-account` falls back to the project's default Compute Engine service account, which typically only has `roles/run.invoker`. The deploy step then fails with `PERMISSION_DENIED` on `run.services.get`. You can confirm which identity holds `run.developer` on the backend with:
  ```bash
  gcloud run services get-iam-policy <BACKEND_SERVICE> --region=<REGION> \
    --format="value(bindings)" | grep run.developer
  ```
  but that check only works for the backend — the frontend has no Cloud Run service to hold an IAM
  policy at all (see **Note the current revisions**), so `gcloud run services get-iam-policy
  <FRONTEND_SERVICE>` returns nothing useful for it even though it also needs a `--service-account`.
  The method that works for both, because it's exactly what the automated deploy already uses, is
  reading it off the trigger itself — see **Finding the values below**. Pass whichever identity you
  find via `--service-account`, as above. This borrows the CI/CD identity for a manual run — that is
  deliberate, not a workaround to route around, since it is the identity actually provisioned to
  deploy this service. If that service account has not been granted read access to Cloud Build's
  default staging bucket (`gs://<PROJECT_ID>_cloudbuild`), the submit will instead fail with
  `storage.objects.get` denied on the uploaded source tarball; grant it once with:
  ```bash
  gcloud storage buckets add-iam-policy-binding gs://<PROJECT_ID>_cloudbuild \
    --member=serviceAccount:<BACKEND_TRIGGER_SA> --role=roles/storage.objectViewer
  ```
  Repeat for the frontend's trigger service account if `cloudbuild-deploy.yaml` hits the same two errors.

**Check what you're about to upload before you submit.** A manual submit tarballs your entire
working directory (`.`), filtered only by `.gcloudignore`. Run `du -sh .` first — this repo's real
source is under 200 MB, so anything close to a gigabyte means something unexpected is being swept
up. `.gcloudignore` excludes build output it knows about (`node_modules`, `dist`, `.venv`, `.git`)
but, confirmed by actually measuring one such upload, it currently misses:

- **`.terraform/`** — Terraform's downloaded provider binaries under `infra/environments/*/`, ~270
  MB per `terraform init` you've run. Not in `.gcloudignore` at all.
- **`_review/`** and **`screenshots/`** — gitignored, but gitignore and gcloudignore are separate
  files and only the latter governs what a build submit uploads. If you keep generated review
  material or screenshots at the repo root, this is where it goes: 236 MB and 69 MB respectively
  in the run that caught this.
- **Any other untracked directory `.gcloudignore` was never told about** — a stray nested clone of
  this same repo (left over from a `git clone` run inside itself) is what triggered this
  investigation, contributing roughly 1.5 GB on its own: a full `.venv` and `.terraform` cache per
  nesting level, plus `backend/bootstrap/assets` (demo seed media, ~85 MB) and a stray
  `cloud-sql-proxy` binary.

The first two are permanent gaps in `.gcloudignore` — add `**/.terraform/`, `_review/`, and
`screenshots/` to it so they stop riding along for everyone, not just this checkout. The third is
checkout-specific junk with no config fix; find it with `du -sh */` and delete or move it out.

### Deploy both services

Frontend-only or backend-only upgrades will leave the two out of step. A backend-only deploy in
particular can leave the old UI offering options the API now rejects.

Your `frontend/src/environments/environment.prod.ts` is gitignored, so your Firebase and backend
configuration survives the upgrade — but the frontend must still be rebuilt for client-side
changes to take effect.

### A manual deploy and an active trigger can fight each other

If you deploy manually — to get an unmerged fix out sooner than a PR review allows, for
example — your Cloud Build trigger is still watching its normal source. Cloud Run gives 100% of
traffic to whichever revision was deployed most recently, so the next automatic deploy silently
overwrites your manual one with whatever the tracked source currently contains.

This matters most if you are running from a fork while a fix sits in an unmerged upstream PR:
syncing that fork with upstream and pushing is the normal way to pick up changes, and doing so
before the PR merges pulls in upstream `main` **without** the fix, then deploys it over your
manual one the moment someone pushes.

If a manual deploy needs to outlive a normal deploy cycle, either disable the trigger for the
duration (`gcloud builds triggers update <name> --disabled`, both backend and frontend), or make
sure whoever can push to the tracked branch knows not to sync with upstream until the real fix
has landed there.

---

## After deploying

1. **Watch the backend start.** Migrations run before the app serves traffic:
   ```bash
   gcloud run services logs read <BACKEND_SERVICE> --region=<REGION> --limit=50
   ```
   If this deploy carried a migration, look for `Migrations applied successfully`. Most deploys
   don't, in which case the healthy message is `Database is already up to date. No pending
   migrations.` instead — that is not a failure, just the other branch of the same check. Either
   way it should be followed by `Application startup complete`.
2. **Sign in** and confirm the gallery loads with thumbnails.
3. **Generate one video** on your most-used model.
4. **Run one saved workflow**, if you use them. See the compatibility note below.

---

## Rolling back

**Backend** rollback is immediate — tested live, including confirming traffic actually moved:

```bash
gcloud run services update-traffic <BACKEND_SERVICE> --region=<REGION> \
  --to-revisions=<PREVIOUS_REVISION>=100
```

**Frontend** does not use this command — it isn't a Cloud Run service (see **Note the current
revisions**), and Firebase Hosting has no equivalent one-line CLI rollback as of this writing. The
supported paths are:
- **Console**: Hosting → Release history → the previous release's ⋮ menu → Roll back. This is
  Firebase's own recommended method.
- **CLI**, if you'd rather script it: `firebase hosting:versions:clone` the prior version into a
  new one, then `firebase deploy --only hosting` to make it live. Cloning alone does not go live.

Not verified live in this pass — the version-clone path needs `firebase-tools` installed and
Firebase-level permissions this session's identity didn't have on the test project. Confirm it
once in a rehearsal before relying on it during a real incident.

If migrations ran and you need to undo them, restore the backup taken in step 2. Do not rely on
`alembic downgrade` for anything destructive.

---

## Compatibility notes for the Gemini Omni changes

### Breaking: requests that used to be accepted are now rejected

Per-model limits are enforced where previously a single set of bounds applied to every model.
These now return HTTP 400 instead of being silently coerced or ignored:

| Request | Why |
|---|---|
| Veo with `duration_seconds` of 5 or 7 | Veo offers 4, 6 and 8 only |
| Gemini Omni with an end frame | Omni cannot do first+last frame interpolation |
| Gemini Omni with a source video for extension | Omni cannot extend video |
| Gemini Omni with an audio reference | The API rejects audio input outright |
| `gemini-omni-generate-preview` as the model | Not a real model; Vertex rejects it |

**Audit saved workflows before upgrading.** The workflow executor posts to the same endpoint and
sends `end_image_asset_id` whenever the step has one, so a saved workflow pairing Omni with an end
frame will begin failing. It was producing incorrect output before — that combination is
interpolation, which Omni does not support — but it now fails visibly instead of quietly.

### No data changes

- **No new migrations.** The new `EDIT_SOURCE` asset role is stored in a JSONB column, not a
  Postgres enum type.
- **Existing media items are untouched.** Nothing rewrites rows.
- **Clips generated before the upgrade remain editable.** They lack the stored interaction steps
  that newer clips carry, so editing them falls back to sending the clip by URI. This fallback
  exists specifically so older library items keep working.
- **Cloud Storage is untouched** by a code deploy.

### An opening frame and reference images can now be combined

**Frames to Video** accepts reference images alongside the opening frame when the model supports
it, which today means Gemini Omni. This is the main consistency workflow for serialized drama:
the frame fixes the composition and the references hold each character's face and wardrobe.

It was previously rejected outright — `"Reference media cannot be used at the same time as a start
frame, end frame, or source video."` That rule is correct for Veo, which types its reference
images separately from its input image and refuses both in one request. Omni has no typed
reference field: every image rides the multimodal input, so the pairing is just an ordered list
and works. Verified live before relaxing it.

The request routes to `task=image_to_video` rather than `reference_to_video`, so the first image
stays the opening frame instead of being demoted to another reference.

`<IMAGE_REF_N>` counts **every** image in the request, and the opening frame is sent first, so it
owns `<IMAGE_REF_0>` and the reference images start at `<IMAGE_REF_1>`. The prompt box numbers the
badges accordingly. `<FIRST_FRAME>` is **not** a recognised tag - a mirrored-pair test bound it to
the wrong image, while swapping two `<IMAGE_REF_N>` indices swapped the performance as instructed. An end frame or an
extension source alongside references is still rejected for every model, Omni included, since
Omni cannot interpolate or extend.

### Where video and image inputs belong

The reference-video slot has been removed from **Ingredients to Video**. Reference videos under
three seconds were accepted by the schema but not processed correctly, and the supported way to
combine a video with images is an edit.

To composite a character into existing footage, use **Edit Video** and attach reference images
alongside the clip. That sends text, image and video together with `task=edit`, matching Google's
Vertex sample. Role tags work here too: `<IMAGE_REF_N>` is positional and counts the reference
images in the order they were attached, so `<IMAGE_REF_0>` is the first image added. Verified
against the live API with three references, in mirrored pairs that exchange the two indices:
the composited arrangement flipped with them every time.

### End an edit prompt with "Keep everything else the same"

Edit prompts that scope the change some other way are frequently rejected with

```
400 Unable to submit request because This model does not support video extension.
```

even though the request asks for `task=edit`. The trigger is the prompt wording, not the request
shape: across 116 live calls, ref-image count, tags versus plain names and the staging described
all made no difference, while the closing sentence decided the outcome. `"Keep the room and
lighting the same."` failed 13/13 on one prompt; the same request ending `"Keep everything else
the same."` succeeded 9/10. Paraphrases are measurably weaker, so prefer that exact sentence.
Appending `"This is an edit of the provided video, not an extension. Do not add any new footage."`
also worked (3/3) where the wording has to stay.

Whether the service literally reclassifies the request as an extension is unverified - that
reading comes from the error text. What is established is that the wording controls it. The
Edit Video snackbar now suggests the working phrasing.

Anyone who previously attached a reference video in Ingredients mode will find the slot gone.
Nothing breaks; the input was not being used properly in the first place.

### Source audio is removed before an edit

Omni refuses to edit a clip carrying speech when reference images are also supplied:

```
The model is currently unable to process speech edits.
```

Every Omni clip has a native audio track, so editing one of its own outputs would fail. The
backend now strips the audio from the source clip first, uploads the silent copy alongside the
original, and sends that. **Remove audio from the source clip** in Edit Video controls this and
defaults on; turn it off to keep the original audio when editing without references.

This writes one extra object per edit under `edit_sources/` in your media bucket. They are small
— the video stream is copied rather than re-encoded — but they accumulate, so a lifecycle rule on
that prefix is worth considering.

`ffmpeg` is required and is already installed in the backend image.

### Behaviour that changes without any action

- Selecting 9:16 now produces a portrait video. It previously returned 16:9 regardless.
- Duration now reaches the model. Clips will match the requested length, where previously the
  value was discarded.
- Gemini Omni can return multiple clips per request, up to 4. **Each is a separate billed
  generation**, so a user selecting x4 spends four times as much.
- Video is delivered to Cloud Storage by URI rather than inline, removing a size ceiling that
  affected longer clips.

## Compatibility notes for the VPE changes

VPE adds video upscaling to the gallery. It is **off by default and inert until configured**, so
upgrading changes nothing for a deployment that does not opt in — the routes still exist, but
every one of them refuses before doing any work.

### Turning it on for the first time, in order

Each step is expanded below. Steps 1 and 2 are prerequisites you cannot work around; the rest is
about fifteen minutes.

1. **Confirm the project is allowlisted** for `veo-experimental`, with the VPE programme. Nothing
   else in this list matters until it is.
2. **Provoke `gcp-sa-vertex-tune` into existing**, once, from the Console: **Vertex AI → Tuning →
   Create tuned model**, start it, cancel it immediately. This cannot be scripted.
3. **Run the bootstrap script** (below). It does everything else in the project, is idempotent,
   and prints the settings filled in.
4. **Add the settings to `be_env_vars`** in your environment's `.tfvars`, under `common` or the
   specific environment. This is the step nothing else hints at — the backend reads them as plain
   Cloud Run environment variables, and they are wired through `be_env_vars` → `platform` →
   `container_env_vars`.
5. **`terraform apply`, then deploy the backend and frontend** as you normally would.
6. **Verify** by opening a 24 fps clip in the gallery and checking the Upscale button is enabled.

To try it locally before any of that, the same keys go in `backend/.env` — the backend reads that
file directly, and the names are case-sensitive.

### No data changes

- **No new migrations.** `veo3p1_upscale` is stored in a plain `String` column and the
  `upscale_source` role in JSON, so neither needs a Postgres enum change. `alembic upgrade head`
  will report nothing new from this branch.
- **Existing media items are untouched.** An upscale writes a new row and never rewrites its
  source.

### You cannot enable this without being allowlisted

The `veo-experimental` endpoint is gated **per calling project**. This is not an IAM problem you
can solve from your side: the project has to be added to the allowlist by the VPE programme.
Until it is, the calls fail in a way that looks like an ordinary permission error.

Note also that `VPE_LOCATION` is deliberately separate from the app's existing `LOCATION`, which
defaults to `global`. Every VPE sample is `us-central1`, and that is the only region this has been
exercised in — reusing `LOCATION` would point VPE at `global` and fail.

### Settings

All six are read at startup by `config_service`, and `require_vpe_configured` refuses the
request up front if any of the three required ones is missing — you get a clear 4xx rather than a
job that dies minutes in.

| Setting | Default | Notes |
|---|---|---|
| `VPE_ENABLED` | `false` | Leave false and nothing below matters |
| `VPE_PROJECT_ID` | *empty* | **The allowlisted project.** May or may not be the project the app runs in |
| `VPE_BUCKET` | *empty* | A bucket **inside** `VPE_PROJECT_ID`. Use a dedicated one — see below |
| `VPE_LOCATION` | `us-central1` | |
| `VPE_DRY_RUN` | `false` | `true` builds and logs the request without calling Vertex or billing |
| `VPE_MAX_CONCURRENT_JOBS` | `3` | |

`VPE_PROJECT_ID` and `VPE_BUCKET` ship empty on purpose. There is no sensible default for either,
and inheriting the app's project would silently produce the cross-project failure below.

**Where they go.** These are ordinary Cloud Run environment variables, set through Terraform's
`be_env_vars` map — `common` for every environment, or a named environment for one:

```hcl
be_env_vars = {
  common = {
    LOG_LEVEL = "INFO"
  }
  production = {
    ENVIRONMENT    = "production"
    VPE_ENABLED    = "true"
    VPE_PROJECT_ID = "<allowlisted-project>"
    VPE_BUCKET     = "<dedicated-bucket>"
    VPE_LOCATION   = "us-central1"
  }
}
```

Terraform values are strings, so `VPE_ENABLED` is `"true"`, not a bare `true`. Pydantic parses it.
`VPE_LOCATION` can be omitted since `us-central1` is already the default; it is listed here
because being explicit costs nothing and the app's separate `LOCATION` defaults to `global`.

### Two projects, or one

The worker holds two Cloud Storage clients — one for the app's own bucket and one for
`VPE_BUCKET` — because VPE requires its input and output buckets to live in the allowlisted
project.

**If the app is deployed into a project that is not allowlisted**, `VPE_PROJECT_ID` names the
allowlisted one and `VPE_BUCKET` must be a bucket inside it. Pointing `VPE_BUCKET` at the app's
bucket in a different project fails at job time, not at startup. The deployed backend's service
account then needs access to **both** projects.

**If the app is deployed into the allowlisted project itself**, set `VPE_PROJECT_ID` to that same
project. Everything works; the two clients simply resolve into one project. Still give VPE its
**own bucket** rather than reusing the app's media bucket, for two reasons that are about blast
radius rather than correctness:

- The three Vertex service agents need `roles/storage.admin` on whatever `VPE_BUCKET` names.
  Pointed at your media bucket, that is full control — including delete — over every user's
  media. A dedicated bucket confines it to scratch data.
- **VPE's outputs are never cleaned up.** The worker deletes the input segments it uploaded once
  a job succeeds, but the upscaled segment VPE wrote under `vpe/upscale/<id>/out_NN/` stays.
  Those are 4K video and they accumulate. A dedicated bucket lets you put an age-based lifecycle
  rule on the whole thing; a shared one does not, because the same rule would reach user media.

All VPE traffic stays under the `vpe/upscale/<media item id>/` prefix, and the finished master is
written to `upscaled_videos/` in the app's bucket, so a shared bucket does not collide — it is
only the two concerns above.

### Prepare the allowlisted project before deploying

**This is the step that will fail if skipped**, and the failure message does not explain itself.
VPE needs three service agents holding `roles/storage.admin` on the I/O bucket, and two of them —
`gcp-sa-vertex-bp` and `gcp-sa-vertex-tune` — **do not exist** until a batch-prediction job and a
tuning job have each been started once in that project. Granting a role to an agent that was
never provisioned returns `Principal does not exist`.

```bash
backend/scripts/vpe_bootstrap_project.sh \
    --project <allowlisted-project> \
    --bucket <bucket-in-that-project> \
    --runtime-sa <the deployed backend's service account> \
    --lifecycle-days 30
```

`--lifecycle-days` is optional but recommended — it bounds the scratch data described above. The
rule is scoped to the `vpe/upscale/` prefix, so it is safe even on a shared bucket, and the script
refuses to touch a bucket that already has a lifecycle policy rather than replacing it.

It enables the APIs, creates or verifies the bucket, provokes the missing agents into existing by
starting and immediately cancelling a throwaway job, grants all three, and prints the settings
above filled in. It is idempotent — run it again after any change and it reports what it found.
`--dry-run` prints every command without touching anything.

One part is not automatable: there is no non-interactive gcloud surface for starting a tuning
job, so `gcp-sa-vertex-tune` has to be provoked once from the Console (**Vertex AI → Tuning →
Create tuned model**, then cancel it). The script detects this, says so, and completes the rest.

### What it costs, and what it does to a clip

- An upscale is **billed per segment**, and a clip longer than 192 frames is split into several.
  A 240-frame clip is two segments, not one.
- Roughly **5 minutes of wall clock per segment**. The UI polls; it does not block.
- Only clips that are **exactly 24 fps**, 96–192 frames, and one of `1280x720`, `720x1280`,
  `1920x1080`, `1080x1920` are eligible. The gallery screens each clip on open and shows the
  Upscale button on every video, enabled only where the screening did not rule it out. Expect
  users to see a disabled button with a tooltip on most existing library clips — a clip that was
  not generated at 24 fps will never be eligible.

### Rolling back

Set `VPE_ENABLED=false` and redeploy. There is no schema to unwind. Media items produced by an
upscale remain in the gallery and stay playable — they are ordinary rows.
