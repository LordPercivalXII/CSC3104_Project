# Submission files (Group 10)

| File | Where it is |
| --- | --- |
| `GROUP10_INSTRUCTIONS.docx` | Here. Export to `GROUP10_INSTRUCTIONS.pdf` for submission. |
| `GROUP10_image.tar` (or `.tar.gz`) | **Not in git** (about 3.5 GB; GitHub rejects files over 100 MB). Submitted separately. |
| `GROUP10.pdf` | The report, exported from the shared Word document. |

## Building the image

From the repository root, after the final code is committed:

```bash
docker build -t smart-gateway:dev .                              # the gateway image
docker build -f Dockerfile.submission -t smart-gateway:dev .     # add the project under /project
docker save smart-gateway:dev | gzip > GROUP10_image.tar.gz      # save outside the synced folder
```

`Dockerfile.submission.dockerignore` keeps `.env`, `.git`, caches and intermediate results out of the image.
`docker load -i GROUP10_image.tar.gz` reads the compressed file directly.
