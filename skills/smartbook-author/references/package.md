# Smartbook package

The intended first format is a ZIP whose root contains `index.html`; HTML may load relative local CSS, JavaScript, images, fonts and other static resources. `smartbook.json` is optional metadata: no chapter schema is currently required. A smartbook is a representation of an existing textbook and is uploaded under that book.

The local packaging tool uses preliminary limits: 50 MiB compressed, 200 MiB expanded, 1,000 files and 32 MiB per file. It permits only ZIP stored/deflate compression and the current backend's static-file extension list. It rejects unsafe or ambiguous archive paths, links, encrypted entries and unsupported extensions. It also checks some static references in `index.html`, but does not inspect all CSS or JavaScript references. These checks are a convenience, not an implementation of the platform's complete security policy. The deployed platform may apply different limits, resource policies or review requirements. Confirm against the running platform and its current documentation before promising upload compatibility.

Run these commands from this skill directory, replacing paths with the authoring project and destination:

```bash
python scripts/smartbook.py pack /path/to/dist /path/to/smartbook.zip
python scripts/smartbook.py validate /path/to/smartbook.zip
```

Windows PowerShell accepts the same commands with Windows paths. Python 3.9 or newer is required; the tool uses only its standard library. Run `python scripts/smartbook.py --help` and each subcommand's `--help` for details. If running from a different working directory, replace `scripts/smartbook.py` with the path to the script in the installed skill directory.

Package only the distribution. Before upload, check the chapter map against the source, review text and media rights, test actual navigation and interactions in the target browser, and resolve reported missing resources or unsupported features. The platform runs deterministic format and security checks and requires administrator manual review. A successful local check does not prove completeness, safety or successful platform publication.
