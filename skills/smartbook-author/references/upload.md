# Upload to an existing textbook

Use `scripts/upload.py` after packaging and browser verification when the author asks to upload. The author must supply the existing book ID, platform origin, and an API key authorized to edit that book. Use an author API key with `book:edit` permission or an authorized account API key. The platform must support API-key uploads to `/v1/author/books/{book_id}/media/smartbook`.

Set `SMARTBOOK_API_KEY` in the harness process environment using its secret-management facility. Do not include the key in command arguments, prompts, project files, logs, screenshots, or the output. Do not ask the author to paste the key into a chat. Run this from the installed skill directory:

```bash
python scripts/upload.py /path/to/smartbook.zip --book-id 123 --base-url http://example.com:5173
```

The tool validates a snapshot of the ZIP and uploads those same bytes with `Authorization: Bearer` and an idempotency key. It prints the idempotency key before the request and returns JSON without printing the API key or the server response body. A successful response includes the book ID, asset ID, SHA-256, and actual review/publication state. A newly uploaded asset normally enters `pending_review`. An upload does not approve or publish the book; independent administrator review and author publication remain separate actions.

Replace the example origin with the platform origin supplied by the author. The tool also accepts HTTPS origins. HTTP sends the API key and book unencrypted and emits a warning for non-loopback hosts. If the author has SSH access to the platform host, a protected local tunnel is an alternative:

```bash
ssh -N -L 127.0.0.1:15173:127.0.0.1:5173 <ssh-host>
```

While the tunnel runs, use `--base-url http://127.0.0.1:15173`. Replace `<ssh-host>` and the ports with the author's actual SSH host and deployment ports. The tool does not follow redirects or automatically use proxy settings.

If a timeout, interrupted connection, 5xx response, or invalid response makes the outcome uncertain, do not start a fresh upload with a new key. Check the author workspace or rerun the exact same ZIP, book ID, and origin using `--idempotency-key` with the printed key. A different ZIP or target requires a new idempotency key. HTTP rejection does not itself establish whether a previous attempt completed.
