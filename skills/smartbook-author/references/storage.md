# Optional reading preferences

When the deployed reader supports `smartbook-storage-v1`, use the included `assets/smartbook-storage.js` to preserve small preferences such as theme and reading position. Copy that file into the book's distribution and load it before the application script. Do not include the skill directory itself in the uploaded book.

```html
<script src="assets/js/smartbook-storage.js"></script>
<script src="assets/js/book.js"></script>
```

Wait for the initial snapshot before reading or writing settings:

```js
async function startBook() {
  await window.SmartbookStorage.ready;
  const theme = window.SmartbookStorage.getItem('book:theme') || 'light';
  applyTheme(theme);
}

startBook();
```

`getItem`, `setItem` and `removeItem` are synchronous against the client's in-memory copy after `ready`. Writes to the parent are asynchronous and unacknowledged, and may be lost if storage is unavailable, the parent does not implement the bridge, authorization changes, limits are exceeded or the page closes before a pending write is sent. `ready` resolves with an empty snapshot after four seconds if the parent does not respond. In an unframed preview, the client attempts browser `localStorage` and tolerates it being unavailable. The platform stores the record for the current account and textbook; this is device/browser-local state, not cloud synchronization.

Keys use 1-64 ASCII letters, digits, `_`, `.`, `:` or `-`; `__proto__`, `prototype` and `constructor` are forbidden. Values are strings of at most 4,096 JavaScript characters. The parent currently allows at most 64 keys and 32,768 UTF-8 bytes for the serialized record; write limits are enforced by the parent without acknowledgement. Do not store tokens, sensitive personal data or irreplaceable work. Design the book to remain usable when preferences cannot be saved.

Bridge availability and limits depend on the deployed platform version. Verify with the actual target reader, including reload and denied-storage conditions; a local preview alone cannot demonstrate integration.
