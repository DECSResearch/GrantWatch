# Grants.gov Document Checker

Temporary document validation pipeline where applicants upload opportunity-specific files. Uploads land in an encrypted S3 bucket, a Lambda function validates them, and a DynamoDB entry tracks checklist status surfaced through the FastAPI backend and Next.js UI.

## Architecture
- **Frontend:** Next.js App Router (`app/page.tsx`) talks to the FastAPI backend via REST.
- **Backend:** FastAPI (`src/web/app.py`) exposes `/start-submission`, `/upload-url`, `/status/{id}`, and `/manifest` endpoints, reading manifests from YAML and storing state in DynamoDB.
- **Storage:** S3 bucket `grant-doc-checker-temp-<env>` with default SSE, blocked public access, lifecycle rule deleting objects after 2 days, and CORS for browser uploads.
- **Processing:** S3 ObjectCreated events flow through EventBridge to the `ValidateDoc` Lambda (`aws/lambda/validate_doc.py`) that runs pdfplumber/Textract checks and updates DynamoDB.
- **State:** DynamoDB table `submissions` keeps per-file findings, overall status, TTL attribute for automatic cleanup.

## FastAPI Endpoints
| Method | Path | Description |
| --- | --- | --- |
| POST | `/start-submission` | Creates a new submission, returns `submission_id`. Optional body `{ "opportunity_id": "opp-001" }`. |
| POST | `/upload-url` | Body `{ filename, contentType, submission_id?, requirement_id, opportunity_id? }`. Returns presigned PUT URL + object key. |
| GET | `/status/{submission_id}` | Returns overall status and per-file messages. |
| GET | `/manifest?opportunity_id=opp-001` | Returns requirement list derived from YAML in `config/doc_manifests`. |
| GET | `/manifest/index` | Lists available opportunity IDs and labels. |

## Environment Variables
Configure these (see `.env.example`):
- `DOC_CHECKER_BUCKET`, `DOC_CHECKER_TABLE`: AWS resource names.
- `DOC_CHECKER_ALLOWED_ORIGINS`: comma-separated origins for CORS (e.g. `http://localhost:3000`).
- `DOC_CHECKER_PRESIGN_SECONDS`, `DOC_CHECKER_TTL_DAYS`, `DOC_CHECKER_DEFAULT_MAX_MB`, `DOC_CHECKER_DEFAULT_MAX_PAGES`.
- `DOC_CHECKER_ENABLE_TEXTRACT`: `true` to invoke Textract when PDF text is empty.
- `DOC_CHECKER_MANIFEST_PATH`: directory containing manifest YAML files (`config/doc_manifests` by default).
- AWS credentials/region (standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`).
- Frontend uses `NEXT_PUBLIC_DOC_CHECKER_API` to find the backend.

## Local Development
1. **Python backend**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows PowerShell
   pip install -r requirements.txt
   uvicorn src.web.app:app --reload
   ```
2. **Next.js frontend**
   ```bash
   npm install
   npm run dev
   ```
   Visit `http://localhost:3000`. The UI will call the backend at `NEXT_PUBLIC_DOC_CHECKER_API` (defaults to `http://localhost:8000`).

## AWS Deployment
1. **Package the Lambda**
   ```powershell
   cd GrantWatch
   python -m venv lambda-env
   lambda-env\Scripts\activate
   pip install -r aws/lambda/requirements.txt -t aws/dist
   Copy-Item aws/lambda/validate_doc.py aws/dist/validate_doc.py
   Copy-Item -Recurse doc_checker aws/dist/doc_checker
   cd aws/dist
   Compress-Archive -Path * -DestinationPath ../validate_doc.zip -Force
   ```
2. **Apply Terraform**
   ```bash
   cd GrantWatch/infrastructure
   terraform init
   terraform apply -var "environment=dev" -var "lambda_package_path=../aws/dist/validate_doc.zip"
   ```
   Terraform provisions the S3 bucket with SSE + 2-day lifecycle, DynamoDB table with TTL, IAM roles/policies for backend and Lambda, EventBridge rule, and Lambda wiring.
3. **Configure FastAPI runtime** with the Terraform outputs (bucket, table) and AWS credentials.

## Validation Flow
1. Start the backend + frontend locally.
2. Pick an opportunity (e.g. `opp-001`) and upload documents. Files stream directly to S3 via presigned PUT URLs.
3. The Lambda runs automatically, updating DynamoDB with validation results (filename regex, size, content type, pages, required sections, optional Textract fallback).
4. Click **Run Checks** to refresh the UI; ✅ indicates pass, ❌/warnings include Lambda messages. Submissions and objects expire automatically after 48 hours.

## Notes
- The manifest loader (`doc_checker/manifest.py`) reads all YAML files within `config/doc_manifests`, so you can add or version requirements without code changes.
- `aws/lambda/validate_doc.py` reuses the shared `doc_checker` package; ensure it is bundled with the Lambda artifact.
- Bucket CORS (configured via Terraform) allows PUT/GET/HEAD from the UI origins for presigned uploads.
- DynamoDB TTL field (`ttl`) plus bucket lifecycle keeps the environment self-cleaning, satisfying the temporary requirement.
