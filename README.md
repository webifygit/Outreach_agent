# Contact-Form + Email Outreach Agent

Reads a spreadsheet of websites. For each one it looks for a contact form, fills
and submits it, and screenshots the evidence. If there is no usable form (or a
CAPTCHA guards it) it falls back to a templated email instead.

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

### Web UI (no command line after this)

```bash
python app.py
```

Open **http://127.0.0.1:5000** - drag in a spreadsheet, flip Live mode on/off,
hit Start. The dashboard on the page updates itself as sites are processed, so
you don't need to re-run anything by hand for each batch. Runs on localhost
only; each new run just launches `run.py` in the background, same as the CLI.

### Command line

```bash
python run.py --input leads.xlsx                      # DRY RUN - default, nothing is sent
python run.py --input leads.xlsx --limit 5 --no-headless   # watch the browser work
export SMTP_PASSWORD_TARANNUM='...' SMTP_PASSWORD_MOHAMMED='...' SMTP_PASSWORD_IRSHAD='...'
python run.py --input leads.xlsx --live               # for real
```

Dry run does everything except click submit and send: it still finds the form,
fills every field, and screenshots the filled state. **Always dry-run 10 rows and
read the screenshots before going live.**

## Input spreadsheet

Column names are matched loosely - `Website Link`, `URL`, `Site` all map to
`website`. Only the first is required.

| column | required | notes |
|---|---|---|
| `website` | yes | `https://` is added if missing |
| `company_name` | no | falls back to the domain name |
| `email` | no | fallback address; if blank the agent scrapes the site for one |
| `contact_name` | no | first name is used in the greeting |
| `notes` | no | any extra columns are also passed to the templates |

`input_sample.xlsx` shows the shape.

## Sender identities

`config.yaml` has one `organization:` block (company, website, pitch - shared)
and **two independent identity pools**, each rotated one-per-row
(`row_index % count`, so the same site always gets the same identity across
resumed runs):

- **`form_senders`** - whose name/email/phone gets typed into a site's contact
  form fields. Not used for sending anything, so these addresses never need an
  app password.
- **`email_senders`** - which Gmail account actually sends the fallback email
  when a site has no usable form. Gmail (and most providers) rejects a From
  address that doesn't match the authenticated account, so **each entry needs
  its own app password**, referenced by `smtp_password_env`:

```bash
export SMTP_PASSWORD_SALES='...'            # webifysales@gmail.com
export SMTP_PASSWORD_TARANNUM_GMAIL='...'   # tarannumfromwebify@gmail.com
```

Every template sees the currently-picked identity as `sender.*` (`sender.name`,
`sender.email`, `sender.company`, `sender.designation`, ...) - which pool it
came from depends on whether the template is being rendered for the form fill
or the email fallback.

## Templates

All Jinja2. Every spreadsheet column is a variable, plus `sender.*` and
`{{ ai_hook }}` (see below).

- `templates/form_message_full.txt.j2` - form message, no length limit
- `templates/form_message_medium.txt.j2` - fits forms capped around ~1000 characters
- `templates/form_message_700.txt.j2` - fits forms capped around ~700 characters
- `templates/form_message_500.txt.j2` - fits forms capped around ~500 characters
- `templates/email_body.txt.j2` - the email fallback (no length limit, so it's full-length)

The agent reads the target form's message-field `maxlength` (when the site
sets one) and automatically uses the largest tier that fits - no limit found
means `full` is used. If even the shortest tier doesn't fit, it's trimmed to
the last full word inside the limit. Tune the mapping in `config.yaml` under
`form.message_tiers`.

## Output

- `output/results.xlsx` - one row per site: method, status, detail, screenshot paths
- `output/report.html` - live dashboard, rewritten after every site (open it in a
  browser, or just use the web UI which embeds it and refreshes automatically)
- `evidence/run_<timestamp>/` - `NNNN_domain_01_filled.png`, `..._02_after_submit.png`,
  and a `.txt` copy of every email body sent
- `output/state.json` - progress; a re-run resumes and skips completed rows

### Status values

| status | meaning |
|---|---|
| `success` | confirmation text or thank-you redirect detected |
| `uncertain` | submitted, but no confirmation found - **open the screenshot** |
| `failed` | the form returned a validation error |
| `sent` | email delivered to the SMTP server |
| `no_contact_found` | no form and no email address anywhere |
| `unreachable` | site did not load |
| `dry_run` | filled but not submitted |

## How form detection works

1. Load the homepage, score every `<form>` on it.
2. If nothing scores well, follow links matching *contact / get in touch /
   enquiry*, then try `/contact`, `/contact-us`, etc.
3. Scoring rejects search boxes, login forms, and single-field newsletter signups.
4. Fields are matched to roles (name, email, phone, company, subject, message)
   from their `name`, `id`, `placeholder`, `aria-label` and `<label>` text.
5. **Hidden fields are never filled** - most are honeypots, and filling one is the
   fastest way to be silently classified as a bot.
6. Required consent checkboxes are ticked; marketing opt-ins are not.

## Local LLM personalization (optional, no API key)

`{{ ai_hook }}` in `email_body.txt.j2` is a placeholder for one personalized
sentence. Point the agent at a locally running [Ollama](https://ollama.com)
model to generate it per company - everything stays on your machine, no API
key involved. (The four form-message tiers use fixed copy and don't include
`ai_hook` - most contact forms are too length-constrained for it to be worth
the words.)

```bash
brew install ollama          # or see ollama.com/download
ollama serve                 # runs a local server on :11434
ollama pull llama3.1         # any model works - pick one that fits your RAM
```

Then in `config.yaml`, set `llm.enabled: true`.

Per site, the agent scrapes the homepage title/meta description/H1, sends it
together with `company_name`, `notes`, and `organization.pitch` to the local
model, and gets back one short sentence that fills `{{ ai_hook }}`. If Ollama
isn't running or the model isn't pulled, this silently falls back to the
generic line already in the template - it never blocks or fails a run.

## Tuning

Everything lives in `config.yaml`: throttling (`delay_between_sites`), the daily
email cap, timeouts, CAPTCHA behaviour, and the `organization`/`senders` identities
used to fill forms and send email.

If a specific site fails, run `--no-headless --limit 1` against it and watch.
Common causes: the form is inside a third-party iframe (HubSpot, Typeform,
Jotform), the page is behind Cloudflare, or the fields are unlabelled.

## Known limits

- **CAPTCHA-protected forms are skipped by design.** The agent falls back to
  email rather than trying to defeat the check.
- **iframe-embedded forms** (HubSpot/Typeform/Jotform) are not filled by the
  current extractor - it only reads the main frame. If many of your targets use
  these, extend `locate_form` to loop over `page.frames`.
- `uncertain` is a real and common outcome. Some sites give no confirmation at
  all. Sample the screenshots rather than trusting the count.
