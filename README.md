# CSC3104_Project

Smart AI gateway: a FastAPI chat service that answers repeated questions from a semantic cache
(Qdrant), sends new ones to an LLM (Gemini) through a fair-share scheduler, and runs on
Docker Compose or Kubernetes.

## Quick start

1. Get a free Gemini API key at https://aistudio.google.com/apikey
2. Copy `.env.example` to `.env` and put the key in it (`.env` is git-ignored; never commit a key).
3. `docker compose up --build`, then open http://localhost:8000

For Kubernetes, see [k8s/README.md](k8s/README.md). For the semantic cache, see
[smart_ai_gateway/semantic_cache_readme.md](smart_ai_gateway/semantic_cache_readme.md).

## Testing from this repository (without the submitted image)

The submission includes a Docker image (`GROUP10_image.tar.gz`) and testing instructions
(`GROUP10_INSTRUCTIONS.pdf`, also in [submission/](submission/)). If the image is not available,
everything can be built from this repository instead. You need Docker (8 GB of memory for Docker),
`kind`, `kubectl` and, on Windows, Git Bash. No API key is needed: without one the gateway uses a mock LLM.

```bash
git clone https://github.com/LordPercivalXII/CSC3104_Project.git
cd CSC3104_Project
bash k8s/up.sh                 # builds smart-gateway:dev from source (first build about 5-10 min),
                               # then creates a 3-node kind cluster and deploys everything
bash k8s/test.sh               # deployment test (about 3 min)
bash k8s/test-scheduler.sh     # distributed scheduler test (about 10 min)
```

Then follow the testing instructions from Part B2, with two differences from the image route:
skip Step 1 (`docker load`), and run `k8s/up.sh` **without** `SKIP_BUILD=1`, as above.
For the Docker Compose demo (Part A), build it from source with
`docker compose -f demo/compose.yaml up -d --build` instead of tagging the loaded image.
The unit tests (Part C) work once `k8s/up.sh` has built the image.

## The LLM

Settings are in the `[llm]` section of [smart_ai_gateway/gateway.config](smart_ai_gateway/gateway.config);
the code is in `smart_ai_gateway/services/llm.py`.

- **Model:** `gemini-3.5-flash-lite`, Gemini's newest Flash-Lite model (October 2026), with
  `thinking_level = minimal` for quick answers. It is fast, has higher free-tier limits than
  the Flash models, and is strong enough for helpdesk questions. Change `model` to switch.
- **No key, or offline:** set `provider = mock` (or the environment variable `GW_LLM_PROVIDER=mock`)
  for instant placeholder answers.
- **Running without Docker:** set the key in the shell first, e.g. in PowerShell
  `$env:GEMINI_API_KEY = "..."`, then `python -m uvicorn main:app --reload`.
- **Failures:** a provider rate limit or full scheduler queue returns HTTP 429 with `Retry-After`;
  an end-to-end request deadline returns 504; other unavailable-provider errors return 503. Nothing is cached.
  Server errors and overload (500, 502, 503) are retried `max_retries` times; timeouts and
  rate limits are not.
- **Cached answers are tagged with the LLM that wrote them**, so after changing `provider` or `model`,
  older answers (including mock ones) are not served and the sweeper deletes them.

### Free tier restrictions

Google does not publish fixed free-tier numbers any more; your project's limits are shown at
https://aistudio.google.com/rate-limit. As of October 2026:

- **Small limits:** third-party reports put Flash-Lite models at about 500 requests per day, and
  the Flash models (e.g. `gemini-3.8-flash`) at only about 5 per minute and 20 per day.
- **Slow and sometimes overloaded:** free-tier requests get the lowest priority. A Flash-Lite answer
  took about 15 seconds in testing, and `gemini-3.8-flash` returned "high demand" errors.
- **Per Google project, not per pod:** all gateway replicas share the same quota.
- **Your prompts may be used by Google** to improve its products. Do not send confidential or
  personal data on the free tier.
- **No Batch, Flex or Priority processing**, and newer Pro models are paid-only.

The semantic cache is what makes the free tier workable: repeated and reworded questions never
reach Gemini.
