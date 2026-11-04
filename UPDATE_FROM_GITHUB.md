# Update the Contact Agent on this Mac from GitHub

**For the Claude agent reading this:** follow these steps in order to pull the
latest Contact Agent code onto this machine. Do exactly what is written. If
anything deviates from what is described here, STOP and ask the user before
continuing — do not improvise, force, or delete anything.

Repo: `webifygit/Outreach_agent`
Branch to use: `agent/human-handoff-truthful-forms` (the latest code is here; it
is **not** on `main` yet)

---

## Step 0 — find the repo and go to it

The repo folder may be in a different place on this Mac. Find it, then `cd` in:

```bash
cd ~/Desktop/Agent/Outreach_agent 2>/dev/null || cd "$(find ~ -type d -name Outreach_agent -maxdepth 5 2>/dev/null | head -1)"
pwd                     # confirm you are inside the Outreach_agent repo
git remote -v           # should show webifygit/Outreach_agent
```

If `pwd` is not an Outreach_agent folder, or the remote is not
`webifygit/Outreach_agent`, STOP and ask the user where the repo is.

---

## Step 1 — check for local uncommitted edits FIRST

```bash
git status
```

- If it says **"nothing to commit, working tree clean"**, or only lists
  *untracked* files (lines starting with `??`), go to Step 2.
- If it lists **modified tracked files** (lines with `M`, in red/not under
  "Untracked files"), this PC has local code edits that were never committed.
  Do NOT overwrite them. Set them aside safely first:

  ```bash
  git stash push -m "local edits before github update"
  ```

  Remember you did this — you will reapply them in Step 4.
  If you are unsure whether it is safe to stash, STOP and show the user the
  `git status` output instead.

---

## Step 2 — pull the latest code

```bash
git fetch origin
git switch agent/human-handoff-truthful-forms
git pull origin agent/human-handoff-truthful-forms
```

`git switch` works whether or not this branch already existed locally. If
`git switch` errors, STOP and show the user the error — do not try `--force`.

---

## Step 3 — confirm it worked

```bash
git log --oneline -3
git status -sb
```

You should see recent commits including the helper scripts + pitch templates,
and the branch tracking `origin/agent/human-handoff-truthful-forms`.

---

## Step 4 — reapply local edits (ONLY if you stashed in Step 1)

```bash
git stash pop
```

If git reports a **merge conflict**, STOP. Do not guess at a resolution —
show the user the conflicting files and ask how to proceed.

---

## Step 5 — register the `webify_general` pitch in this machine's config (one-time)

The `webify_general` templates arrive via the pull, but each machine's run
config is gitignored and local, so the script must be registered here once.
Skip this if it is already done (the check below tells you).

1. Find which config file on this machine already holds the other Webify
   scripts, and whether `webify_general` is already in it:

   ```bash
   grep -l 'webify_hire_developers' config*.yaml 2>/dev/null
   grep -l 'webify_general' config*.yaml 2>/dev/null
   ```

   - If `webify_general` already appears in every file that has
     `webify_hire_developers`, it is already registered — you are done, skip
     to the test.
   - Otherwise, for each config file that has `webify_hire_developers` but NOT
     `webify_general`, do step 2.

2. In that file, under the `scripts:` list, add the block below immediately
   **before** the `- key: general` line. Keep the two-space indentation exactly
   as shown. For `senders:`, use the SAME sender keys the other `webify_*`
   scripts in that same file use (look at `webify_hire_developers`' `senders:`
   line and copy it) — do NOT invent names.

   ```yaml
     - key: webify_general
       label: "WEBIFY - general digital growth (Webify.ai)"
       blurb: "Digital marketing, web/app development & AI solutions for any business"
       senders: [irshad, tarannum]
       form:
         subject_line: "Helping {{ company_name or 'your business' }} reach more potential customers"
         message_tiers:
           full: "templates/webify_general/form_message_full.txt.j2"
           medium: "templates/webify_general/form_message_medium.txt.j2"
           short: "templates/webify_general/form_message_700.txt.j2"
           shortest: "templates/webify_general/form_message_500.txt.j2"
       email:
         subject_template: "Helping {{ company_name or 'your business' }} reach more potential customers"
         body_template_path: "templates/webify_general/email_body.txt.j2"
         html_template_path: ""
   ```

3. Confirm the file still parses and the script resolves:

   ```bash
   PYTHONPATH=. python3 - <<'PY'
   import yaml, glob
   for cf in glob.glob("config*.yaml"):
       d = yaml.safe_load(open(cf))
       keys = [s["key"] for s in (d.get("scripts") or [])]
       print(cf, "webify_general" in keys)
   PY
   ```

   If `yaml.safe_load` raises, your indentation is wrong — fix it; do not leave
   the file broken.

Do NOT commit or push these config edits — the config files are gitignored
and hold this machine's real sender details. They stay local.

---

## Safety rules — do not break these

1. **This PC's own identity and data files are gitignored and must stay that
   way. A pull will not touch them, and you must never commit or push them:**
   - `config.local.yaml`, `config.pest.yaml`, `config.pest_la.yaml`,
     `config.targeted.yaml` — this machine's real sender identities
     (names, emails, phone)
   - `.env.local` — API tokens / passwords
   - `output*/`, `evidence/`, `uploads/`, and any `leads_*.xlsx` / lead lists
     — this machine's run history and prospect data
   If `git status` ever shows one of these as a file git wants to add, STOP:
   the `.gitignore` is wrong on this machine and the user must be told.

2. **Do NOT push, merge, or change `main`** from this machine as part of an
   update. Pulling only.

3. **Do NOT run any live outreach** as part of updating. Updating code and
   running the agent are separate actions. If the user wants a run afterwards,
   that is a new, explicit request — and keep `run.mode: dry_run` unless the
   user says otherwise.

4. **Do NOT solve CAPTCHAs or bypass Cloudflare / bot detection.** Those sites
   are handed to a person via the human queue. This is intentional.

5. If any command fails twice, or anything is unexpected, STOP and explain to
   the user what you tried and what happened. Do not keep retrying.

---

## What this update contains (for context)

Helper scripts and the approved pitch templates
(`pest_control_*`, `webify_dental_marketing`, `webify_hire_developers*`),
plus a hardened `.gitignore`. No identities, credentials, or lead data are in
the repo — those live only in the gitignored files on each machine.
