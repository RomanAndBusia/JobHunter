# JobHunter

JobHunter is a local, single-user application for discovering vacancies on Just Join IT, comparing them with a CV, and preparing applications in a visible Google Chrome window. It also accepts individual vacancy URLs and follows employer and applicant-tracking-system forms.

You review the prepared application before approving its final submission. JobHunter records submission only after it observes success on the website.

## Features

- Just Join IT search with source filters, authenticated recommendations, persistent search progress, and duplicate detection.
- PDF and DOCX CV import, versioned documents, structured candidate profiles, and evidence attached to candidate facts.
- Individual vacancy matching with `APPLY`, `SKIP`, and `NEEDS_REVIEW` decisions. Direct applications retain suitability as advisory information.
- Visible browser preparation across native and custom form controls, uploads, conditional fields, repeated sections, iframes, and multi-step forms.
- Questions for missing personal facts, application-scoped answers, and explicitly confirmed reusable memory.
- Optional cover letters grounded in the selected profile and CV.
- Application queue, review, pause/resume, retry, reset, history, and runtime diagnostics.
- Persistent browser login state and manual completion of authentication and CAPTCHA challenges.

Website layouts change. Some forms need manual input or may be unsupported; inspect the live page during preparation and review.

## Technology stack

| Component | Technologies |
| --- | --- |
| Backend | Python 3.9, FastAPI, Uvicorn, Pydantic |
| Persistence | SQLAlchemy, Alembic, SQLite |
| Browser | Playwright connected to installed Google Chrome over loopback CDP |
| Document extraction | pypdf and Python's DOCX/XML processing |
| AI | Structured provider interface, OpenAI Responses API, optional local CLI runtime |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, TanStack Query, Zustand |
| Tests | pytest, pytest-asyncio, local HTML browser fixtures |

## Installation

Windows is the supported launcher environment. Python modules retain Python 3.9 compatibility. The release is validated with Python 3.9 and Node.js 24.

Install these prerequisites:

- Python **3.9**, including the Windows `py` launcher.
- Node.js and npm (Node.js 24 is tested).
- Google Chrome. A separate Playwright browser download is unnecessary.
- Git to clone the repository.
- An AI provider credential for profile extraction, matching, and generated answers.

In **Command Prompt**:

```cmd
git clone https://github.com/RomanAndBusia/JobHunter.git
cd JobHunter
copy .env.example .env
setup.bat
```

Edit the root `.env` before using AI features. `setup.bat` creates `backend\.venv`, installs the pinned backend dependencies and the frontend lockfile, checks Chrome, and initializes or migrates the database. It preserves an existing `.env`.

For manual installation, from the repository root:

```cmd
py -3.9 -m venv backend\.venv
call backend\.venv\Scripts\activate.bat
py -m pip install -r backend\requirements.txt
cd backend
py -m app.db.migrate
cd ..\frontend
npm ci
cd ..
```

Copy `.env.example` to `.env` first if you have not already done so.

## Configuration

The backend reads **only the repository-root `.env`**; process environment variables override it. A `backend\.env` is not loaded. Restart the backend after configuration changes. Keep keys and candidate information out of source control.

The example contains no credentials. Choose `JOBHUNTER_AI_MODEL` for the model access available to your provider account; the example model is configurable and availability is not guaranteed.

### AI provider

| Variable | Purpose |
| --- | --- |
| `AI_RUNTIME` | `api` (default) or `codex`. |
| `OPENAI_API_KEY` | Required for `api`, or when API fallback is explicitly enabled. |
| `JOBHUNTER_AI_PROVIDER` | `openai` for the API runtime. |
| `JOBHUNTER_AI_MODEL` | Model used by the API provider. |
| `AI_RUNTIME_FALLBACK` | `none` by default; `api` explicitly permits API fallback. |
| `JOBHUNTER_OPENAI_BASE_URL` | Must remain `https://api.openai.com/v1` for API mode. |
| `JOBHUNTER_AI_REASONING_EFFORT` | Reasoning setting supported by the chosen model. |
| `JOBHUNTER_AI_BATCH_SIZE`, `JOBHUNTER_AI_MAX_OUTPUT_TOKENS` | Matching batch and response-size limits. |
| `JOBHUNTER_OPENAI_TIMEOUT_SECONDS`, `JOBHUNTER_OPENAI_MAX_RETRIES` | API timeout and retry limits. |
| `JOBHUNTER_CODEX_EXECUTABLE`, `JOBHUNTER_CODEX_TIMEOUT_SECONDS` | Executable and bounded timeout for the optional runtime. |

The optional `AI_RUNTIME=codex` integration runs an installed, authenticated Codex CLI through bounded `codex exec` calls. It is a product runtime option; API credentials are needed only if API fallback is enabled. Both runtimes use the same application review and submission controls.

Without a key, API mode starts and exposes a clear `MissingAPIKey` diagnostic. AI-dependent work cannot complete until configured. Malformed runtime/provider configuration stops startup with a configuration error.

CV text, relevant candidate facts, vacancy text, and form context are sent to the selected AI runtime for the requested operation. API calls may incur provider charges. Review your provider's data handling before importing personal documents.

### Local services and browser

| Variable | Purpose |
| --- | --- |
| `JOBHUNTER_DATABASE_URL` | Default `sqlite:///./jobhunter.db`, relative to the backend working directory. |
| `JOBHUNTER_FRONTEND_URL` | Default `http://127.0.0.1:5173`; also used for CORS. |
| `JOBHUNTER_BACKEND_URL` | Default `http://127.0.0.1:8000`; determines backend host/port. |
| `JOBHUNTER_LOG_LEVEL` | Default `INFO`. |
| `JOBHUNTER_BROWSER_MODE`, `JOBHUNTER_CDP_HOST` | Keep `cdp` and `127.0.0.1`. |
| `JOBHUNTER_CHROME_PATH` | Optional installed Chrome executable when automatic discovery fails. |
| `JOBHUNTER_CHROME_PROFILE` | Dedicated persistent profile; relative paths resolve from the repository root. |
| `JOBHUNTER_BROWSER_AUTOSTART` | Open/attach the visible browser on startup; default `true`. |
| `JOBHUNTER_CHALLENGE_TIMEOUT_SECONDS`, `JOBHUNTER_CHALLENGE_POLL_SECONDS` | Bounded waiting for manual challenge completion. |
| `JOBHUNTER_COVER_LETTER_MODE` | `off`, `auto` (default), or `always`. |
| `JOBHUNTER_AUTO_ACCEPT_REQUIRED_RECRUITMENT_CONSENTS` | Leave `false`; consent and legal declarations need user evidence. |

The Vite development proxy targets backend port **8000** in `frontend/vite.config.ts`. If you change the backend port, update that proxy too. Set the frontend URL consistently if changing its port.

### Search, salary, and guarded runs

| Variable | Purpose |
| --- | --- |
| `JOBHUNTER_SCANNER_CONCURRENCY`, `JOBHUNTER_SCANNER_DELAY_SECONDS` | Discovery concurrency and pacing. |
| `JOBHUNTER_HTTP_TIMEOUT_SECONDS` | Source request timeout. |
| `JOBHUNTER_RAW_HTML_RETENTION_HOURS` | Retention setting for diagnostic HTML. |
| `SALARY_STRATEGY`, `SALARY_EXPERIENCE_TEXT_ENABLED` | Salary-answer policy; the example uses `lowest_available`. |
| `SALARY_FALLBACK_MONTHLY_PLN`, `SALARY_FALLBACK_HOURLY_PLN` | Optional explicit fallbacks; blank values leave unknown facts for you to answer. |
| `LIVE_APPLICATION_TEST`, `ALLOW_FINAL_SUBMIT` | Use `true` / `false` for guarded preparation with final actions blocked. |

Normal defaults are `LIVE_APPLICATION_TEST=false` and `ALLOW_FINAL_SUBMIT=true`; explicit review and approval are still required. Applications created as tests remain blocked from submission after flags change.

## Usage

From the repository root:

```cmd
start.bat
```

Open [the local dashboard](http://127.0.0.1:5173). The launcher checks dependencies, migrates the database, and starts both services. **It stops processes listening on the configured backend/frontend ports**, so reserve those ports for JobHunter. Logs are stored under `storage\logs`.

1. Open Settings and check AI Diagnostics. Connection and production matching tests make real provider requests.
2. Import your CV under Candidate profile or Documents. Review extracted facts and confirm corrections; inferred skills are not silently confirmed.
3. Open Jobs to search Just Join IT or load Recommendations. Sign in manually in the dedicated Chrome window when prompted.
4. Select vacancies and a CV before preparation, or use Apply by URL for an individual vacancy.
5. Answer missing-fact questions in the queue or application detail. Answers stay application-scoped unless you explicitly choose global scope.
6. Compare Application Review with the live form, correct it as needed, then explicitly approve submission.

Authentication and CAPTCHA challenges remain visible for manual completion. JobHunter waits and resumes when they clear; it does not bypass them. Closing a live application tab can interrupt its session. Shutting down the backend disconnects while leaving Chrome open.

For separate development terminals, activate the virtual environment and start the backend:

```cmd
call backend\.venv\Scripts\activate.bat
cd backend
py -m app.server
```

In a second terminal, from the repository root:

```cmd
cd frontend
npm run dev
```

`scripts\dev.cmd` also starts both services after setup. Use `app.server` on Windows so Playwright runs with the correct event loop. Do not enable Uvicorn reload.

The local API exposes interactive documentation at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Project structure

```text
backend/
  app/
    ai/                 Structured provider integrations
    applications/       Lifecycle, preparation, review, submission
    browser/            Chrome ownership, inspection, verified controls
    candidate/          Profiles, documents, facts, memory
    forms/              Semantic form planning and identity
    jobs/               Vacancy extraction and normalization
    matching/           Candidate-specific evaluation
    scanner/            Source integrations
    search/             Discovery and campaign runners
    api/ db/ models/    HTTP endpoints and persistence
  alembic/              Database migrations
  tests/                Unit/integration tests and synthetic HTML
  requirements.txt
frontend/
  src/                  React application
  package.json
  package-lock.json
scripts/                Windows setup and launchers
test/                   Legacy CDP fixture harness used by regression tests
.env.example
setup.bat
start.bat
```

Databases, uploaded documents, derived CVs, generated letters, logs, screenshots, browser profiles, dependencies, and build outputs are created locally and excluded from Git.

## Tests and build

From the repository root in Command Prompt:

```cmd
call backend\.venv\Scripts\activate.bat
cd backend
py -m pytest -q
cd ..\frontend
npm run build
```

The backend tests use temporary/in-memory databases and synthetic forms. Browser tests launch visible installed Chrome with disposable profiles; no job application is submitted. They require Chrome and may open windows. Unit tests mock provider calls and do not require a real API key.

Python 3.9 compatibility limits some dependency updates. The release dependency audit still flags `starlette`, `python-multipart`, `python-dotenv`, and the test dependency `pytest`; their remaining upstream fixes require Python 3.10 or newer. This release does not claim a clean Python vulnerability audit. Keep this application local, avoid untrusted environment files and test code, and reassess these dependencies before any deployment beyond the supported single-user setup.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Wrong Python version | Run `py -0p`, install Python 3.9, and create a Python 3.9 virtual environment. Setup/start reject another version. |
| Chrome is not found | Install Google Chrome or set `JOBHUNTER_CHROME_PATH` to its executable in the root `.env`. |
| Browser/async subprocess startup fails on Windows | Start with `py -m app.server`; avoid Uvicorn reload. Inspect Settings browser diagnostics. |
| `MissingAPIKey` or model access error | Check the root `.env`, selected runtime/model, provider account access, and AI Diagnostics. Restart after edits. |
| Dashboard cannot reach API | Check both services and ports. The Vite `/api` proxy must match the backend port. |
| SQLite reports locking | Stop duplicate backends using the same database. Keep each test or development instance on its own database. |
| Application needs user input or verification | Inspect its inline questions and owned browser tab, answer/complete the challenge, then resume if needed. |
| A DOCX CV cannot be uploaded to a PDF-only field | Select a PDF CV. Optional DOCX-to-PDF adaptation requires installed Microsoft Word on Windows and verifies the resulting text. |
| Upgrade has missing tables/columns | With the venv active, run `py -m app.db.migrate` from `backend`. Back up your private database first. |

## Privacy and deployment

JobHunter is intended for one person on their own computer. It has no user authentication or multi-user authorization layer. Keep the dashboard, API, and Chrome debugging endpoint on loopback; do not expose them to a public network.

The database contains candidate facts and application history. Chrome's dedicated profile contains website sessions. Treat all local storage, environment files, documents, and logs as private. Do not commit or attach them to public issues. Share only redacted diagnostic excerpts.

## License

No project license file is currently provided. Licensing terms have not been selected.
