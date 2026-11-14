# Smartbook package

The first reader format is an offline, single-page ZIP with `index.html` at its root. A smartbook represents an existing textbook and is uploaded under that book; `smartbook.json` is optional metadata and no chapter schema is required.

The archive limits are 50 MiB compressed, 16 MiB expanded, and 1,000 entries. `index.html` is limited to 2 MiB and 20,000 DOM nodes with depth at most 128. At most 256 local resource references are allowed. The estimated inlined document must be at most 24 MiB: index bytes plus `4 * ceil(resource bytes / 3) + 128` for each reference, including repeated references. The reader separately checks the final serialized document size. CSS is limited to 20,000 comments per stylesheet or inline block. Use ZIP stored/deflate compression and relative paths, without `?`, `#`, `%`, backslashes, dot segments or whitespace at either end. Do not include links, encrypted entries or nested archives.

The entry page can use classic JavaScript (`<script src="assets/app.js"></script>`), local UTF-8 CSS stylesheets, and local images/audio/video via `src`; all referenced files must be inside the ZIP. An icon link must use a `data:image/` URL. CSS `url()`, `@import` and backslash escapes are unsupported, including in inline styles. Module scripts, forms, nested frames, `template`, `base`, `object`, `embed`, `meta http-equiv`, `srcset`, `formaction`, `poster`, duplicate attributes and self-closing script/style tags are unsupported. Navigation inside the book uses `#` anchors, not other HTML pages or external links. Runtime network access is disabled; bundle content and initialize it locally. Do not depend on dynamically constructed resource URLs, module imports or persistent browser storage; validate and exercise the actual book in the deployed reader.

The tool checks ZIP structure and static references in `index.html`, including linked CSS. It does not evaluate JavaScript or prove runtime completeness, security or successful publication. Platform validation and administrator review are authoritative, and the deployed version may differ.

Run these commands from this skill directory, replacing paths with the authoring project and destination:

```bash
python scripts/smartbook.py pack /path/to/dist /path/to/smartbook.zip
python scripts/smartbook.py validate /path/to/smartbook.zip
```

Windows PowerShell accepts the same commands with Windows paths. Python 3.9 or newer is required; the tool uses only its standard library. Run `python scripts/smartbook.py --help` and each subcommand's `--help` for details. If running from a different working directory, replace `scripts/smartbook.py` with the path to the script in the installed skill directory.

Package only the distribution. Before upload, check the chapter map against the source, review text and media rights, test actual navigation and interactions in the target browser, and resolve reported missing resources or unsupported features. The platform runs deterministic format and security checks and requires administrator manual review. A successful local check does not prove completeness, safety or successful platform publication.
